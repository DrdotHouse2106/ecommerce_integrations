"""Drop ``Item Group.shopware_priority`` — dead field.

The category-order-by-priority feature it fed (``sync_category_order_by_priority``
/ ``_reorder_children_by_priority`` in ``shopware6/export/category_handler.py``)
was removed as unused; no code reads this field anymore. Left in place it
was just a confusing, do-nothing "Priorität (Shopware)" field on every
Item Group form.

No data migration: the field never had a working consumer, so there's
nothing meaningful to preserve before dropping it.
"""

import frappe


def execute() -> None:
	if not frappe.db.exists("DocType", "Item Group"):
		return
	if not frappe.db.exists("Custom Field", "Item Group-shopware_priority"):
		return

	frappe.delete_doc("Custom Field", "Item Group-shopware_priority", ignore_permissions=True)
	frappe.db.commit()  # noqa: SLF001
