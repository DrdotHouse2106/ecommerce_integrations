"""
Custom Fields for Shopware 6 Integration

Defines custom fields added to standard ERPNext doctypes.
"""

import frappe

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
            "fieldname": "category_image",
            "label": "Category Image (Shopware)",
            "fieldtype": "Attach Image",
            "insert_after": "shopware_active",
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
            "fieldname": "shopware_properties_section",
            "label": "Shopware Properties",
            "fieldtype": "Section Break",
            "insert_after": "shopware_selling_rate",
            "collapsible": 0,
            "description": "Flexible key-value table for Shopware properties and custom fields",
        },
        {
            "fieldname": "shopware_properties",
            "label": "Properties",
            "fieldtype": "Table",
            "options": "Item Shopware Property",
            "insert_after": "shopware_properties_section",
            "description": "Add properties here. Type 'Property' = filterable in shop, 'Custom Field' = product detail page only",
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
        {
            "fieldname": "shopware_custom_section",
            "label": "Shopware Custom Fields",
            "fieldtype": "Section Break",
            "insert_after": "shopware_erpnext_mode_of_payment",
            "collapsible": 1,
        },
        {
            "fieldname": "customer_po_no",
            "label": "Customer PO Number",
            "fieldtype": "Data",
            "insert_after": "shopware_custom_section",
            "read_only": 0,
            "translatable": 0,
            "description": "Customer's internal order reference / commission number",
        },
        {
            "fieldname": "tel_avis_requested",
            "label": "Tel. Avis Requested",
            "fieldtype": "Check",
            "insert_after": "customer_po_no",
            "read_only": 0,
            "default": 0,
            "description": "Telephone notification before delivery (+7.50 EUR)",
        },
        {
            "fieldname": "shopware_custom_cb",
            "label": "",
            "fieldtype": "Column Break",
            "insert_after": "tel_avis_requested",
        },
        {
            "fieldname": "forklift_requested",
            "label": "Forklift Requested",
            "fieldtype": "Check",
            "insert_after": "shopware_custom_cb",
            "read_only": 0,
            "default": 0,
            "description": "Forklift/lifting platform required for delivery",
        },
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
