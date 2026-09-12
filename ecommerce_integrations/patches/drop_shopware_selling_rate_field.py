"""Drop ``Item.shopware_selling_rate`` — dead field.

The Shopware price sync has always read ``standard_rate`` directly
(``ITEM_SELLING_RATE_FIELD`` in ``shopware6/constants.py``); this Currency
custom field was never read anywhere. Left in place it was just a
confusing, do-nothing second price field on every Item form.

No data migration: the field never had a working consumer, so there's
nothing meaningful to preserve before dropping it.
"""

import frappe


def execute() -> None:
	if not frappe.db.exists("DocType", "Item"):
		return
	if not frappe.db.exists("Custom Field", "Item-shopware_selling_rate"):
		return

	frappe.delete_doc("Custom Field", "Item-shopware_selling_rate", ignore_permissions=True)
	frappe.db.commit()  # noqa: SLF001
