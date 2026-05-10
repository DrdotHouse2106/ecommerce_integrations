"""Install custom fields for Medusa integration on ERPNext DocTypes."""
import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from ecommerce_integrations.ecommerce_integrations.ecommerce_custom_fields import (
    ECOMMERCE_DOWNSTREAM_FIELDS,
    ECOMMERCE_SALES_ORDER_FIELDS,
)

MEDUSA_CUSTOM_FIELDS = {
    "Item Attribute": [
        {
            "fieldname": "sync_to_medusa",
            "label": "Sync to Medusa",
            "fieldtype": "Check",
            "default": "0",
            "insert_after": "numeric_values",
            "description": "Sync this attribute as a Medusa Product Option (creates variants)",
        },
        {
            "fieldname": "medusa_property_type",
            "label": "Medusa Property Type",
            "fieldtype": "Select",
            "options": "\nOption\nMetadata",
            "default": "Option",
            "insert_after": "sync_to_medusa",
            "depends_on": "eval:doc.sync_to_medusa",
            "description": "Option = creates product variants (filterable). Metadata = stored as product metadata (not filterable).",
        },
    ],
    "Customer": [
        {
            "fieldname": "medusa_customer_id",
            "label": "Medusa Customer ID",
            "fieldtype": "Data",
            "read_only": 1,
            "insert_after": "customer_name",
            "unique": 1,
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
        {
            "fieldname": "medusa_payment_provider",
            "label": "Medusa Payment Provider",
            "fieldtype": "Data",
            "read_only": 1,
            "insert_after": "medusa_order_id",
            "no_copy": 1,
            "print_hide": 1,
            "description": "Original payment provider id from Medusa (e.g. pp_stripe_stripe).",
        },
        *ECOMMERCE_SALES_ORDER_FIELDS,
    ],
    "Sales Invoice": [*ECOMMERCE_DOWNSTREAM_FIELDS],
    "Delivery Note": [*ECOMMERCE_DOWNSTREAM_FIELDS],
    "Payment Entry": [*ECOMMERCE_DOWNSTREAM_FIELDS],
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
