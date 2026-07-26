"""Re-run add_shopware_item_custom_field_mappings so its Section/Table
labels (originally installed in English: "Item → Shopware Custom Field
Mapping" / "Item Custom Field Mappings") reach sites that already
consumed that one-time patch — translated to German, matching the rest
of the Shopware Setting form.

Idempotent: create_custom_fields(update=True) upserts label/description
on the existing Custom Field rows without touching stored mapping data.
"""

import frappe


def execute() -> None:
    if not frappe.db.exists("DocType", "Shopware Setting"):
        return
    from ecommerce_integrations.patches.add_shopware_item_custom_field_mappings import (
        execute as reinstall,
    )

    reinstall()
