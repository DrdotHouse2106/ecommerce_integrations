"""Re-sync Shopware custom fields onto existing sites so the new
``Item.restock_time`` field (added alongside ``default_delivery_time`` /
``default_restock_time`` on Shopware Setting) reaches sites that already
consumed the earlier ``add_shopware_description_override_field`` /
``update_shopware_custom_fields`` patches.

Idempotent: ``create_custom_fields`` (called inside
``setup_custom_fields``) upserts by fieldname.
"""

import frappe


def execute() -> None:
    if not frappe.db.exists("DocType", "Item"):
        return
    from ecommerce_integrations.shopware6.custom_fields import setup_custom_fields

    setup_custom_fields()
