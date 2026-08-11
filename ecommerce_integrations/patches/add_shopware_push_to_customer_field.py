"""Re-sync Shopware custom fields onto existing sites so the new
``Customer.push_to_shopware`` field (the opt-in for the ERPNext-to-Shopware
customer push) reaches sites that already consumed earlier
``update_shopware_custom_fields``-family patches.

Idempotent: ``create_custom_fields`` (called inside ``setup_custom_fields``)
upserts by fieldname.
"""

import frappe


def execute() -> None:
	if not frappe.db.exists("DocType", "Customer"):
		return
	from ecommerce_integrations.shopware6.custom_fields import setup_custom_fields

	setup_custom_fields()
