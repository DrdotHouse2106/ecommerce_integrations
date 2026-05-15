# Copyright (c) 2024, Frappe Technologies and contributors
# For license information, please see license.txt
"""
Setup custom fields for Multi-Storefront support.

Run via:
    bench --site <sitename> execute ecommerce_integrations.shopware6.setup_custom_fields.setup
"""

import frappe

# Only the NEW Multi-Storefront fields
MULTI_STOREFRONT_FIELDS = {
    "Item": [
        {
            "fieldname": "shopware_channels_section",
            "label": "Shopware Sales Channels",
            "fieldtype": "Section Break",
            "insert_after": "ecommerce_properties",
            "collapsible": 1,
            "description": "Override which Sales Channels this product is visible in",
        },
        {
            "fieldname": "shopware_use_item_group_channels",
            "label": "Use Item Group Channels",
            "fieldtype": "Check",
            "insert_after": "shopware_channels_section",
            "default": "1",
            "description": "If checked, use the channel assignment from Item Group. Uncheck to override.",
        },
        {
            "fieldname": "shopware_all_channels",
            "label": "Visible in ALL Channels",
            "fieldtype": "Check",
            "insert_after": "shopware_use_item_group_channels",
            "default": "0",
            "depends_on": "eval:!doc.shopware_use_item_group_channels",
            "description": "Make this product visible in every Sales Channel",
        },
        {
            "fieldname": "shopware_channel_overrides",
            "label": "Channel Overrides",
            "fieldtype": "Table",
            "options": "Item Sales Channel Override",
            "insert_after": "shopware_all_channels",
            "depends_on": "eval:!doc.shopware_use_item_group_channels && !doc.shopware_all_channels",
            "description": "Specify exactly which channels this product should appear in",
        },
    ],
    "Customer": [
        {
            "fieldname": "shopware_source_sales_channel_id",
            "label": "Source Sales Channel ID",
            "fieldtype": "Data",
            "insert_after": "shopware_customer_id",
            "read_only": 1,
            "translatable": 0,
            "hidden": 1,
        },
        {
            "fieldname": "shopware_source_sales_channel_name",
            "label": "Source Sales Channel",
            "fieldtype": "Data",
            "insert_after": "shopware_source_sales_channel_id",
            "read_only": 1,
            "translatable": 0,
            "description": "Which shop this customer first registered from",
        },
    ],
    "Sales Order": [
        {
            "fieldname": "shopware_sales_channel_id",
            "label": "Sales Channel ID",
            "fieldtype": "Data",
            "insert_after": "shopware_order_number",
            "read_only": 1,
            "translatable": 0,
            "hidden": 1,
        },
        {
            "fieldname": "shopware_sales_channel_name",
            "label": "Sales Channel",
            "fieldtype": "Data",
            "insert_after": "shopware_sales_channel_id",
            "read_only": 1,
            "translatable": 0,
            "description": "Which shop this order came from",
        },
    ],
}


def setup():
    """Create custom fields for Multi-Storefront support."""
    from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

    create_custom_fields(MULTI_STOREFRONT_FIELDS, update=False)
    frappe.db.commit()
    print("Custom fields created successfully!")
