"""Consolidate the scattered Shopware/AI-description Item fields under a
single "E-Commerce" tab instead of Details/Accounting.

Before this patch, ``shopware_topseller``/``delivery_time``/``restock_time``/
the Ecommerce-Properties + Shopware-Sales-Channels sections lived wedged
into the Details tab (right after ``standard_rate``), the cross-backend
``ecommerce_channel_overrides`` table sat next to them, and the entire
AI-Generated-Description block lived at the *top* of the unrelated
Accounting tab (an artifact of once being inserted after native
``description``, which happens to be the last field before the Accounting
tab break). None of that was easy to find on the Item form.

Frappe only recomputes a Custom Field's ``idx`` (render position) from its
``insert_after`` when the field doc ``is_new()`` — updating ``insert_after``
on an already-existing field and saving is a no-op (see
``force_reposition_item_custom_field_mappings`` for the same lesson learned
earlier). So relocating these already-installed fields means deleting and
recreating them, not just editing their ``insert_after`` value.

Safe for data: deleting a ``Custom Field`` record only removes the field
*definition*. Table-type fields (``ecommerce_properties``,
``shopware_channel_overrides``, ``ecommerce_channel_overrides``) keep their
child-table rows — those live in the child doctype's own table, linked by
``parentfield``/``parenttype``, independent of the Custom Field doc — and
immediately re-link once the field is recreated with the same fieldname.

Sources the exact field definitions from ``shopware6.custom_fields`` and
``ai_description.custom_fields`` (including the new ``ecommerce_tab`` Tab
Break, added there alongside this patch) rather than duplicating them here,
so this patch and a from-scratch install can never drift out of sync.

No-op safe: returns early when Item isn't installed, or when neither
module contributed any Item fields (e.g. a Medusa-only site without the
Shopware/AI-description modules).
"""

import frappe


def execute() -> None:
	if not frappe.db.exists("DocType", "Item"):
		return

	from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

	from ecommerce_integrations.ai_description.custom_fields import CUSTOM_FIELDS as AI_FIELDS
	from ecommerce_integrations.shopware6.custom_fields import CUSTOM_FIELDS as SHOPWARE_FIELDS

	shopware_item_fields = [dict(f) for f in SHOPWARE_FIELDS.get("Item", [])]
	ai_item_fields = [dict(f) for f in AI_FIELDS.get("Item", [])]

	if not shopware_item_fields and not ai_item_fields:
		return

	# Cross-backend table (owned by add_item_ecommerce_channel_overrides,
	# not a per-integration custom_fields.py) — redefined here so it can
	# be slotted between the two blocks in one atomic recreate.
	ecommerce_channel_overrides_field = {
		"fieldname": "ecommerce_channel_overrides",
		"label": "Channel Visibility Overrides (cross-backend)",
		"fieldtype": "Table",
		"options": "Ecommerce Channel Override",
		"insert_after": "shopware_channel_overrides",
		"description": (
			"Per-item overrides for which Shopware/Medusa sales channels "
			"this item appears in. Highest priority — wins over Catalog "
			"Mirror and Smart Collections."
		),
	}

	# ai_description_section's own stored insert_after ("description") is
	# only meaningful for a from-scratch install's very first migrate —
	# pin it explicitly here so it lands right after the channel overrides
	# table regardless of where it started.
	if ai_item_fields:
		ai_item_fields[0] = {**ai_item_fields[0], "insert_after": "ecommerce_channel_overrides"}

	all_fields = [*shopware_item_fields, ecommerce_channel_overrides_field, *ai_item_fields]

	for field in all_fields:
		name = f"Item-{field['fieldname']}"
		if frappe.db.exists("Custom Field", name):
			frappe.delete_doc("Custom Field", name, ignore_permissions=True)

	frappe.clear_cache(doctype="Item")

	create_custom_fields({"Item": all_fields}, update=True)
	frappe.db.commit()  # noqa: SLF001
	frappe.clear_cache(doctype="Item")
