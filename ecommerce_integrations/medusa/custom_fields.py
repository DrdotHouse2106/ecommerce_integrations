"""Install custom fields for Medusa integration on ERPNext DocTypes."""
import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

MEDUSA_CUSTOM_FIELDS = {
    "Customer": [
        {
            "fieldname": "medusa_customer_id",
            "label": "Medusa Customer ID",
            "fieldtype": "Data",
            "read_only": 1,
            "insert_after": "customer_name",
            "no_copy": 1,
            "print_hide": 1,
        },
    ],
    "Sales Order": [
        {
            "fieldname": "medusa_order_id",
            "label": "Medusa Order ID",
            "fieldtype": "Data",
            "read_only": 1,
            "insert_after": "title",
            "no_copy": 1,
            "print_hide": 1,
        },
    ],
    "Item": [
        {
            "fieldname": "medusa_product_id",
            "label": "Medusa Product ID",
            "fieldtype": "Data",
            "read_only": 1,
            "insert_after": "item_name",
            "no_copy": 1,
            "print_hide": 1,
        },
        {
            "fieldname": "medusa_variant_id",
            "label": "Medusa Variant ID",
            "fieldtype": "Data",
            "read_only": 1,
            "insert_after": "medusa_product_id",
            "no_copy": 1,
            "print_hide": 1,
        },
    ],
}


def setup_custom_fields():
    create_custom_fields(MEDUSA_CUSTOM_FIELDS, update=True)
