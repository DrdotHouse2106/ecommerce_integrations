"""Convert legacy Item Group Channel Mappings into Smart Collections.

Reads ``Medusa Setting.item_group_channel_mappings`` and
``Shopware Setting.item_group_channel_mappings`` (plus the Shopware
``all_channels_item_groups`` MultiSelect) and produces one Smart
Collection per (item_group, sales_channel) pair, plus one per item group
listed in all_channels_item_groups (with one target per active channel).

Idempotent: matched by ``slug`` (deterministic from item_group and
backend), so re-running this patch updates the existing rows rather than
creating duplicates. Safe to run on a site without the legacy mappings —
returns early when neither setting exists.

Migration strategy: the generated collections are activated
(``is_active=1``) so the cut-over to Smart-Collections-driven sync is
seamless. Legacy data on the Setting docs stays in place for rollback.
"""

import frappe

from ecommerce_integrations.smart_collections.doctype.ecommerce_smart_collection.ecommerce_smart_collection import (
	slugify,
)


SHOPWARE_VISIBILITY_DEFAULT = "All (30)"


def execute():
	if frappe.db.exists("DocType", "Medusa Setting"):
		_migrate_medusa()
	if frappe.db.exists("DocType", "Shopware Setting"):
		_migrate_shopware()


def _ensure_collection(slug: str, title: str) -> "frappe.Document":
	if frappe.db.exists("Ecommerce Smart Collection", slug):
		coll = frappe.get_doc("Ecommerce Smart Collection", slug)
	else:
		coll = frappe.new_doc("Ecommerce Smart Collection")
		coll.slug = slug
	coll.title = title
	coll.is_active = 1
	coll.allow_empty = 0
	if not coll.rule_combinator:
		coll.rule_combinator = "AND"
	return coll


def _set_item_group_rule(
	coll, item_group: str, include_subcategories: bool, manufacturer_filter: str | None,
	manufacturer_filter_mode: str | None,
) -> None:
	"""Replace the collection's rules with the legacy mapping's intent."""
	coll.rules = []
	op = "descends_from" if include_subcategories else "equals"
	coll.append(
		"rules",
		{
			"rule_type": "Item Group",
			"operator": op,
			"value": item_group,
			"group_id": 0,
			"negate": 0,
		},
	)
	if manufacturer_filter:
		# Shopware mode is "Include Only" or "Exclude" — Medusa's older
		# mapping only carried the value (Include Only by default).
		mode = (manufacturer_filter_mode or "Include Only").strip()
		negate = 1 if mode == "Exclude" else 0
		coll.append(
			"rules",
			{
				"rule_type": "Manufacturer",
				"operator": "in",
				"value": manufacturer_filter,
				"group_id": 0,
				"negate": negate,
			},
		)


def _replace_target(coll, backend: str, sales_channel: str, visibility: str) -> None:
	# Drop any existing target for this (backend, sales_channel) pair so a
	# re-run of the patch doesn't accumulate duplicates.
	new_targets = [
		t for t in (coll.targets or [])
		if not (t.backend == backend and t.sales_channel == sales_channel)
	]
	coll.targets = []
	for t in new_targets:
		coll.append("targets", t.as_dict())
	coll.append(
		"targets",
		{
			"backend": backend,
			"sales_channel": sales_channel,
			"enabled": 1,
			"visibility": visibility,
			"sync_status": "pending",
		},
	)


def _save(coll) -> None:
	coll.flags.ignore_mandatory = True
	coll.flags.ignore_permissions = True
	coll.save()


def _migrate_medusa() -> None:
	if not frappe.db.exists("Medusa Setting", "Medusa Setting"):
		return
	setting = frappe.get_doc("Medusa Setting", "Medusa Setting")
	created = 0
	for row in setting.item_group_channel_mappings or []:
		if not (row.item_group and row.sales_channel):
			continue
		slug = _slug_for_row("medusa", row.item_group, row.sales_channel)
		title = f"{row.item_group} ({row.sales_channel})"
		coll = _ensure_collection(slug, title)
		_set_item_group_rule(
			coll,
			row.item_group,
			bool(row.include_subcategories),
			(row.manufacturer_filter or None),
			None,
		)
		_replace_target(coll, "Medusa", row.sales_channel, SHOPWARE_VISIBILITY_DEFAULT)
		_save(coll)
		created += 1
	if created:
		print(f"Migrated {created} Medusa Item Group Channel rows to Smart Collections")


def _migrate_shopware() -> None:
	if not frappe.db.exists("Shopware Setting", "Shopware Setting"):
		return
	setting = frappe.get_doc("Shopware Setting", "Shopware Setting")

	created = 0
	for row in setting.item_group_channel_mappings or []:
		channel_id = getattr(row, "sales_channel_id", None)
		if not (row.item_group and channel_id):
			continue
		slug = _slug_for_row("shopware", row.item_group, channel_id)
		channel_name = getattr(row, "sales_channel_name", "") or channel_id
		title = f"{row.item_group} ({channel_name})"
		coll = _ensure_collection(slug, title)
		_set_item_group_rule(
			coll,
			row.item_group,
			bool(row.include_subcategories),
			(getattr(row, "manufacturer_filter", None) or None),
			getattr(row, "manufacturer_filter_mode", None),
		)
		_replace_target(
			coll,
			"Shopware",
			channel_id,
			(getattr(row, "visibility", None) or SHOPWARE_VISIBILITY_DEFAULT),
		)
		_save(coll)
		created += 1

	# all_channels_item_groups → one collection per item group with a
	# Target for every active Shopware sales channel.
	all_channel_ids = [
		ch.sales_channel_id
		for ch in (setting.sales_channels or [])
		if getattr(ch, "active", 1)
	]
	for entry in setting.all_channels_item_groups or []:
		ig = getattr(entry, "item_group", None)
		if not ig or not all_channel_ids:
			continue
		slug = _slug_for_row("shopware", ig, "all-channels")
		title = f"{ig} (All Channels)"
		coll = _ensure_collection(slug, title)
		_set_item_group_rule(coll, ig, True, None, None)
		for channel_id in all_channel_ids:
			_replace_target(coll, "Shopware", channel_id, SHOPWARE_VISIBILITY_DEFAULT)
		_save(coll)
		created += 1

	if created:
		print(f"Migrated {created} Shopware mapping rows to Smart Collections")


def _slug_for_row(backend: str, item_group: str, channel: str) -> str:
	"""Deterministic slug so re-runs match existing collections."""
	return slugify(f"{backend} {item_group} {channel}")
