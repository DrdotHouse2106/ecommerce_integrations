# Copyright (c) 2024, Frappe Technologies and contributors
# For license information, please see license.txt
"""
Migration script for Multi-Storefront / Multi-Sales-Channel support.

This script sets up the necessary DocTypes, Custom Fields, and initial configuration
for supporting multiple Shopware Sales Channels in ERPNext.

Run manually via:
    bench --site <sitename> execute ecommerce_integrations.shopware6.migrations.multi_channel_setup.execute
"""

import frappe
from frappe import _


def execute():
    """
    Execute the multi-channel setup migration.

    Steps:
    1. Ensure custom fields are created
    2. Fetch sales channels from Shopware (if integration is enabled)
    3. Mark first channel as default
    """
    print("Starting Multi-Channel Setup Migration...")

    # Step 1: Ensure custom fields are created
    print("1. Creating/Updating Custom Fields...")
    create_custom_fields()

    # Step 2: Fetch sales channels from Shopware
    print("2. Fetching Sales Channels from Shopware...")
    fetch_sales_channels()

    print("Multi-Channel Setup completed successfully!")


def create_custom_fields():
    """Create custom fields for Item, Sales Order, and Customer."""
    from ecommerce_integrations.shopware6.custom_fields import CUSTOM_FIELDS
    from frappe.custom.doctype.custom_field.custom_field import create_custom_fields as frappe_create_custom_fields

    try:
        frappe_create_custom_fields(CUSTOM_FIELDS, update=True)
        frappe.db.commit()
        print("   Custom fields created/updated.")
    except Exception as e:
        print(f"   Warning: Could not create custom fields: {e}")


def fetch_sales_channels():
    """Fetch sales channels from Shopware API and populate the table."""
    from ecommerce_integrations.shopware6.constants import SETTING_DOCTYPE

    try:
        setting = frappe.get_doc(SETTING_DOCTYPE)

        if not setting.is_enabled():
            print("   Shopware integration is not enabled. Skipping channel fetch.")
            return

        setting.fetch_sales_channels()
        print(f"   Fetched {len(setting.sales_channels)} Sales Channel(s).")

    except Exception as e:
        print(f"   Warning: Could not fetch Sales Channels: {e}")
        print("   You can fetch them later via the Shopware Settings form.")


def rollback():
    """Rollback the multi-channel setup (for testing purposes)."""
    print("Rolling back Multi-Channel Setup...")

    # Remove custom fields
    custom_fields_to_remove = [
        "Item-shopware_channels_section",
        "Item-shopware_use_item_group_channels",
        "Item-shopware_all_channels",
        "Item-shopware_channel_overrides",
        "Customer-shopware_source_sales_channel_id",
        "Customer-shopware_source_sales_channel_name",
        "Sales Order-shopware_sales_channel_id",
        "Sales Order-shopware_sales_channel_name",
    ]

    for field_name in custom_fields_to_remove:
        if frappe.db.exists("Custom Field", field_name):
            frappe.delete_doc("Custom Field", field_name, force=True)
            print(f"   Removed: {field_name}")

    frappe.db.commit()
    print("Rollback completed.")
