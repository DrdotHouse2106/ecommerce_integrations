"""Drop the legacy Medusa ID custom fields on ``tabItem``.

Removes ``Item.medusa_product_id`` and ``Item.medusa_variant_id`` once
``migrate_medusa_ids_to_ecommerce_item`` has copied their data into
``tabEcommerce Item``. The custom fields are obsolete because every
read/write site in the Medusa module is now wired to the canonical
mapping table (``integration='medusa'``).

Idempotent and self-protecting:
- No-op when Medusa is not installed on this site.
- Verifies the backfill ran successfully (``tabEcommerce Item`` row
  count for ``integration='medusa'`` is at least the count of legacy
  ``tabItem`` rows). Skips silently when the counts don't agree — losing
  data is worse than leaving a redundant column behind.
- Wraps the DDL in try/except so a re-run after the columns are gone is
  a no-op.
"""

import frappe


def execute():
	if not frappe.db.exists("DocType", "Medusa Setting"):
		return

	# If the columns were already dropped, there's nothing left to do.
	cols = {
		r[0]
		for r in frappe.db.sql(
			"""SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS
			WHERE TABLE_SCHEMA = DATABASE()
			  AND TABLE_NAME = 'tabItem'
			  AND COLUMN_NAME IN ('medusa_product_id', 'medusa_variant_id')"""
		)
	}
	if not cols:
		# Custom Field rows might still exist even when the columns have
		# been dropped (rare partial-rollback case) — clean those up so
		# the doctype stays consistent.
		_delete_custom_field_rows()
		return

	# Verify the backfill ran successfully: every ``tabItem`` row that
	# has either a Medusa product id or a Medusa variant id must have
	# a matching ``tabEcommerce Item`` row for ``integration='medusa'``.
	missing = frappe.db.sql(
		"""SELECT COUNT(*)
		FROM `tabItem` it
		LEFT JOIN `tabEcommerce Item` ei
		  ON ei.integration = 'medusa'
		 AND ei.erpnext_item_code = it.name
		WHERE ei.name IS NULL
		  AND (
		      (it.medusa_product_id IS NOT NULL AND it.medusa_product_id != '')
		   OR (it.medusa_variant_id IS NOT NULL AND it.medusa_variant_id != '')
		  )"""
	)[0][0]
	if missing:
		target_count = frappe.db.sql(
			"SELECT COUNT(*) FROM `tabEcommerce Item` WHERE integration='medusa'"
		)[0][0]
		print(
			f"drop_medusa_item_custom_fields: SKIPPING — backfill incomplete "
			f"({missing} source rows still missing a target; current target={target_count}). "
			f"Re-run migrate_medusa_ids_to_ecommerce_item first."
		)
		return

	# Drop Custom Field rows for both fields first so Frappe stops
	# treating the columns as live custom fields, then drop the columns
	# themselves.
	_delete_custom_field_rows()

	for col in ("medusa_product_id", "medusa_variant_id"):
		if col not in cols:
			continue
		try:
			frappe.db.sql_ddl(f"ALTER TABLE `tabItem` DROP COLUMN `{col}`")
			print(f"drop_medusa_item_custom_fields: dropped `tabItem`.`{col}`")
		except Exception as e:
			# Column already gone (race with a concurrent run) is fine.
			msg = str(e).lower()
			if "check that column/key exists" in msg or "unknown column" in msg:
				continue
			print(
				f"drop_medusa_item_custom_fields: DDL drop of `{col}` failed: {e}"
			)

	# Clear the doctype-meta cache so frappe stops emitting the dropped
	# fields in select queries.
	frappe.clear_cache(doctype="Item")


def _delete_custom_field_rows() -> None:
	for fieldname in ("medusa_product_id", "medusa_variant_id"):
		name = frappe.db.get_value(
			"Custom Field", {"dt": "Item", "fieldname": fieldname}
		)
		if name:
			try:
				frappe.delete_doc(
					"Custom Field", name, ignore_permissions=True, force=True
				)
			except Exception as e:
				print(
					f"drop_medusa_item_custom_fields: could not delete Custom Field "
					f"{name}: {e}"
				)
