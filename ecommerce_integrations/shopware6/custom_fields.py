"""
Custom Fields for Shopware 6 Integration

Defines custom fields added to standard ERPNext doctypes.
"""

import frappe

from ecommerce_integrations.ecommerce_integrations.ecommerce_custom_fields import ECOMMERCE_SALES_ORDER_FIELDS

CUSTOM_FIELDS = {
    "Item Group": [
        {
            "fieldname": "shopware_section",
            "label": "Shopware Settings",
            "fieldtype": "Section Break",
            "insert_after": "is_group",
            "collapsible": 1,
        },
        {
            "fieldname": "shopware_active",
            "label": "Active in Shopware",
            "fieldtype": "Check",
            "insert_after": "shopware_section",
            "default": 1,
            "description": "If unchecked, this category will be set to inactive in Shopware (hidden from customers). This only affects Shopware visibility, not ERPNext.",
        },
        {
            "fieldname": "shopware_priority",
            "label": "Priority (Shopware)",
            "fieldtype": "Int",
            "insert_after": "shopware_active",
            "default": "0",
            "description": "Sort priority for this category in Shopware. Lower numbers appear first. Used by frontend for sorting.",
        },
        {
            "fieldname": "category_image",
            "label": "Category Image (Shopware)",
            "fieldtype": "Attach Image",
            "insert_after": "shopware_priority",
            "description": "Image displayed for this category in Shopware. If empty, the standard Item Group image will be used.",
        },
    ],
    "Item": [
        {
            "fieldname": "shopware_selling_rate",
            "label": "Shopware Selling Rate",
            "fieldtype": "Currency",
            "insert_after": "standard_rate",
            "read_only": 0,
            "translatable": 0,
        },
        # Note: SEO fields (seo_title, seo_meta_description, seo_keywords, delivery_time)
        # are expected to already exist in ERPNext Item doctype.
        # They are synced to Shopware via product_export.py

        # Shopware Properties Table (Key-Value pairs for flexible property management)
        # Replaces old individual custom fields (shopware_zubehoer, jattr_*, etc.)
        {
            "fieldname": "shopware_topseller",
            "label": "Topseller (Shopware)",
            "fieldtype": "Check",
            "insert_after": "shopware_selling_rate",
            "default": "0",
            "description": "Markiert dieses Produkt als Topseller im Shop (gelbes 'Tipp'-Badge)",
        },
        {
            "fieldname": "ecommerce_properties_section",
            "label": "Ecommerce Properties",
            "fieldtype": "Section Break",
            "insert_after": "shopware_selling_rate",
            "collapsible": 0,
            "description": "Universal property table for all ecommerce integrations (Shopware, Medusa, etc.)",
        },
        {
            "fieldname": "ecommerce_properties",
            "label": "Properties",
            "fieldtype": "Table",
            "options": "Item Ecommerce Property",
            "insert_after": "ecommerce_properties_section",
            "description": "Add properties here. Enable sync checkboxes to control which integrations receive each property.",
        },
        # Multi-Storefront / Sales Channel settings
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
            "fieldname": "shopware_customer_id",
            "label": "Shopware Customer ID",
            "fieldtype": "Data",
            "insert_after": "customer_name",
            "read_only": 1,
            "translatable": 0,
        },
        {
            "fieldname": "invoice_email",
            "label": "Invoice Email",
            "fieldtype": "Data",
            "insert_after": "email_id",
            "read_only": 0,
            "translatable": 0,
            "options": "Email",
            "description": "Alternative email address for invoices (from Shopware checkout)",
        },
        # Multi-Storefront: Track which Sales Channel the customer came from
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
        # XRechnung / B2G Leitweg-ID intentionally NOT added here —
        # ``eu_einvoice`` (Alyf) already ships the canonical fields:
        # ``Customer.electronic_address_scheme`` (Link to Common Code,
        # set to ``0204`` for German Leitweg-ID) plus
        # ``Customer.electronic_address`` (Data, the actual Leitweg-ID
        # string). The Shopware Checkout Field mapping with key
        # ``leitweg_id`` routes the value into ``electronic_address``
        # via ``Customer Update``; the EAS scheme is auto-set to
        # ``0204`` by ``shopware6.customer.sync._ensure_leitweg_eas_scheme``.
    ],
    "Sales Order": [
        {
            "fieldname": "shopware_section",
            "label": "Shopware 6",
            "fieldtype": "Section Break",
            "insert_after": "tax_id",
            "collapsible": 1,
        },
        {
            "fieldname": "shopware_order_id",
            "label": "Shopware Order ID",
            "fieldtype": "Data",
            "insert_after": "shopware_section",
            "read_only": 1,
            "translatable": 0,
        },
        {
            "fieldname": "shopware_order_number",
            "label": "Shopware Order Number",
            "fieldtype": "Data",
            "insert_after": "shopware_order_id",
            "read_only": 1,
            "translatable": 0,
        },
        {
            "fieldname": "shopware_column_break",
            "label": "",
            "fieldtype": "Column Break",
            "insert_after": "shopware_order_number",
        },
        {
            "fieldname": "shopware_payment_method",
            "label": "Shopware Payment Method",
            "fieldtype": "Data",
            "insert_after": "shopware_column_break",
            "read_only": 1,
            "translatable": 0,
            "description": "Original payment method from Shopware",
        },
        {
            "fieldname": "shopware_payment_status",
            "label": "Shopware Payment Status",
            "fieldtype": "Data",
            "insert_after": "shopware_payment_method",
            "read_only": 1,
            "translatable": 0,
            "description": "Payment status from Shopware (Paid, Unpaid, etc.)",
        },
        {
            "fieldname": "shopware_erpnext_mode_of_payment",
            "label": "Mode of Payment (Mapped)",
            "fieldtype": "Link",
            "options": "Mode of Payment",
            "insert_after": "shopware_payment_status",
            "read_only": 1,
            "translatable": 0,
            "description": "ERPNext Mode of Payment mapped from Shopware payment method",
        },
        *ECOMMERCE_SALES_ORDER_FIELDS,
    ],
    "Delivery Note": [
        {
            "fieldname": "shopware_delivery_id",
            "label": "Shopware Delivery ID",
            "fieldtype": "Data",
            "insert_after": "title",
            "read_only": 1,
            "translatable": 0,
        },
    ],
    "Sales Invoice": [
        {
            "fieldname": "shopware_transaction_id",
            "label": "Shopware Transaction ID",
            "fieldtype": "Data",
            "insert_after": "title",
            "read_only": 1,
            "translatable": 0,
        },
    ],
}


def setup_custom_fields():
    """Create all custom fields for Shopware integration."""
    from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

    create_custom_fields(CUSTOM_FIELDS)


def remove_custom_fields():
    """Remove custom fields (for uninstall)."""
    for doctype, fields in CUSTOM_FIELDS.items():
        for field in fields:
            fieldname = field.get("fieldname")
            if frappe.db.exists("Custom Field", {"dt": doctype, "fieldname": fieldname}):
                frappe.delete_doc("Custom Field", f"{doctype}-{fieldname}")
