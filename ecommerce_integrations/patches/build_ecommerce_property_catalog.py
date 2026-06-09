"""Build the Ecommerce Property Group catalog from the legacy per-Item rows.

Walks ``tabItem Ecommerce Property``, dedups by ``property_name``,
and creates one ``Ecommerce Property Group`` per distinct name.
Group attributes (``property_type`` / ``filterable`` /
``field_data_type`` / ``sync_to_shopware`` / ``sync_to_medusa``) are
resolved from the rows that referenced this name:

* ``property_type`` — most-common wins. Conflicts are appended to the
  Group's ``notes`` so the operator can review which items disagree.
* ``filterable`` — most-common.
* ``field_data_type`` — most-common non-empty.
* ``sync_to_shopware`` / ``sync_to_medusa`` — OR. Conservative: if
  *any* item ever pushed this property to a backend, the Group keeps
  pushing.

``display_order`` is seeded from the legacy
:func:`product_sync.engine.property_classifier.property_priority`
regex so the post-migration canonical hash matches the pre-migration
hash on items whose data didn't change (the hash is a function of the
positions, but the positions are identical to what the apply pipeline
was already passing to Shopware as ``property_group.position``).

Per-Group ``options`` are populated from the distinct
``property_value`` strings observed for that name, in
descending-frequency order (most popular value first), with
``display_order = idx * 10`` for cheap insert-between later.

Finally each ``Item Ecommerce Property`` row gets its
``property_group`` link backfilled to point at the new Group with the
matching ``property_name``.

Idempotency contract:
* Re-running the patch is a no-op when every distinct ``property_name``
  already has a Group with the right link backfilled.
* If new property_names appeared since the last run (operator
  imported new items), the patch picks them up. Existing Groups are
  left alone — operator edits to ``display_order`` / display labels /
  filterable / sync toggles are not overwritten.

No-op safe:
* Skips entirely if either the new doctype or the legacy table is
  missing on this site.
"""

from __future__ import annotations

from collections import Counter

import frappe


GROUP_DOCTYPE = "Ecommerce Property Group"
ROW_DOCTYPE = "Item Ecommerce Property"


def _legacy_priority(name: str) -> int:
	"""Seed display_order from the regex classifier — keeps post-migration
	canonical hash bit-identical to what the apply pipeline was already
	pushing as ``property_group.position`` pre-migration."""
	try:
		from ecommerce_integrations.product_sync.engine.property_classifier import (
			property_priority,
		)
	except Exception:  # noqa: BLE001
		return 800
	return property_priority(name)


def _aggregate_rows() -> dict[str, dict]:
	"""Return ``{property_name: {agg fields}}`` from the legacy table."""
	rows = frappe.db.sql(
		"""
		SELECT
			property_name,
			property_type,
			property_value,
			COALESCE(field_data_type, '') AS field_data_type,
			COALESCE(filterable, 0) AS filterable,
			COALESCE(sync_to_shopware, 0) AS sync_to_shopware,
			COALESCE(sync_to_medusa, 0) AS sync_to_medusa
		FROM `tabItem Ecommerce Property`
		WHERE parenttype = 'Item' AND property_name IS NOT NULL AND property_name != ''
		""",
		as_dict=True,
	)
	by_name: dict[str, dict] = {}
	for r in rows:
		name = r["property_name"]
		bucket = by_name.setdefault(name, {
			"types": Counter(),
			"filterable": Counter(),
			"field_data_type": Counter(),
			"sync_to_shopware": False,
			"sync_to_medusa": False,
			"values": Counter(),
			"total": 0,
		})
		bucket["types"][r["property_type"] or "Property"] += 1
		bucket["filterable"][int(r["filterable"] or 0)] += 1
		if r["field_data_type"]:
			bucket["field_data_type"][r["field_data_type"]] += 1
		bucket["sync_to_shopware"] = bucket["sync_to_shopware"] or bool(int(r["sync_to_shopware"] or 0))
		bucket["sync_to_medusa"] = bucket["sync_to_medusa"] or bool(int(r["sync_to_medusa"] or 0))
		if r["property_value"]:
			bucket["values"][r["property_value"]] += 1
		bucket["total"] += 1
	return by_name


def _format_conflict(name: str, counter: Counter, field: str) -> str | None:
	"""Return a one-line conflict note when the counter has >1 key."""
	if len(counter) <= 1:
		return None
	picked, _ = counter.most_common(1)[0]
	distribution = ", ".join(f"{k}={v}" for k, v in counter.most_common())
	return f"[{field}] picked={picked!r}; observed: {distribution}"


def _upsert_group(name: str, agg: dict) -> tuple[str, list[str]]:
	"""Create the Group if it doesn't exist; return (group_name, conflict_notes)."""
	conflicts: list[str] = []
	if frappe.db.exists(GROUP_DOCTYPE, name):
		# Idempotent: leave operator edits alone. Only ensure that
		# every distinct value observed is present as an Option (so
		# new values from a re-import slot into the catalog without
		# manual work).
		doc = frappe.get_doc(GROUP_DOCTYPE, name)
		existing_values = {opt.value for opt in (doc.options or [])}
		new_values = [v for v, _ in agg["values"].most_common() if v not in existing_values]
		if new_values:
			next_idx = (max((opt.display_order or 0) for opt in (doc.options or [])) // 10) + 1
			for i, v in enumerate(new_values):
				doc.append("options", {
					"value": v,
					"display_order": (next_idx + i) * 10,
				})
			doc.save(ignore_permissions=True)
		return name, conflicts

	prop_type, _ = agg["types"].most_common(1)[0]
	if c := _format_conflict(name, agg["types"], "property_type"):
		conflicts.append(c)

	filt_flag, _ = agg["filterable"].most_common(1)[0]
	if c := _format_conflict(name, agg["filterable"], "filterable"):
		conflicts.append(c)

	fdt = ""
	if agg["field_data_type"]:
		fdt, _ = agg["field_data_type"].most_common(1)[0]
		if c := _format_conflict(name, agg["field_data_type"], "field_data_type"):
			conflicts.append(c)

	doc = frappe.new_doc(GROUP_DOCTYPE)
	doc.property_name = name
	doc.display_order = _legacy_priority(name)
	doc.property_type = prop_type
	doc.field_data_type = fdt or "text"
	doc.filterable = int(bool(filt_flag))
	doc.sync_to_shopware = int(agg["sync_to_shopware"])
	doc.sync_to_medusa = int(agg["sync_to_medusa"])
	if conflicts:
		doc.notes = "Migration conflicts:\n  " + "\n  ".join(conflicts)
	# Options in descending-frequency order — most popular value first.
	for idx, (value, _) in enumerate(agg["values"].most_common(), start=1):
		doc.append("options", {
			"value": value,
			"display_order": idx * 10,
		})
	doc.insert(ignore_permissions=True)
	return doc.name, conflicts


def _backfill_links() -> int:
	"""Set ``property_group`` to match ``property_name`` everywhere."""
	count = frappe.db.sql(
		"""
		UPDATE `tabItem Ecommerce Property` r
		JOIN `tabEcommerce Property Group` g
		  ON g.property_name = r.property_name
		SET r.property_group = g.name
		WHERE r.parenttype = 'Item'
		  AND (r.property_group IS NULL OR r.property_group = '' OR r.property_group != g.name)
		""",
	)
	return frappe.db.sql("SELECT ROW_COUNT()")[0][0]


def execute():
	# No-op safe: skip when either side of the migration is absent.
	if not frappe.db.exists("DocType", GROUP_DOCTYPE):
		return
	if not frappe.db.exists("DocType", ROW_DOCTYPE):
		return

	# Ensure the property_group column exists before the UPDATE below
	# touches it — bench migrate runs the doctype JSON sync before this
	# patch, but in a rollback/re-run scenario the column might be
	# absent if someone hand-edited the JSON.
	if not frappe.db.has_column(ROW_DOCTYPE, "property_group"):
		return

	by_name = _aggregate_rows()
	if not by_name:
		return

	created = 0
	existed = 0
	total_conflicts = 0
	for name, agg in by_name.items():
		if frappe.db.exists(GROUP_DOCTYPE, name):
			existed += 1
		_, conflicts = _upsert_group(name, agg)
		if name in by_name and conflicts:
			total_conflicts += 1
		if not frappe.db.exists(GROUP_DOCTYPE, name):
			# defensive — _upsert_group failed silently
			continue
		# Only newly-created paths bump created
	created = len(by_name) - existed

	frappe.db.commit()
	backfilled = _backfill_links()
	frappe.db.commit()

	print(
		f"[build_ecommerce_property_catalog] groups: created={created} "
		f"existed={existed} conflicts_logged={total_conflicts} "
		f"item_rows_backfilled={backfilled}"
	)
