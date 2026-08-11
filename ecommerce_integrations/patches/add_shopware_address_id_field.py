"""Re-sync Shopware custom fields onto existing sites so the new
``Address.shopware_address_id`` field reaches sites that already consumed
the earlier ``update_shopware_custom_fields`` patch.

Without this field, ``ShopwareCustomer._update_existing_address()`` runs a
raw SQL query filtering on ``a.shopware_address_id`` (``customer/core.py``)
the moment a returning customer's Shopware address id differs from the one
already stored — a real, common case (e.g. PayPal address changes between
orders). On a site missing the column, that raises
``OperationalError: Unknown column 'a.shopware_address_id'``.

Idempotent: ``create_custom_fields`` (called inside ``setup_custom_fields``)
upserts by fieldname.
"""

import frappe


def execute() -> None:
	if not frappe.db.exists("DocType", "Address"):
		return
	from ecommerce_integrations.shopware6.custom_fields import setup_custom_fields

	setup_custom_fields()
