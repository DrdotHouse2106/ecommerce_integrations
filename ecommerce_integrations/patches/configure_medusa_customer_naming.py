"""Switch Selling Settings to Naming Series for Customer and refresh Medusa custom fields.

Without naming series, ERPNext stores the customer_name (often the email) as
the Customer.name. With naming series, Customer.name becomes a stable id
(CUST-2026-00001) and customer_name remains the human-facing label, free to
duplicate. The Medusa integration relies on a unique medusa_customer_id custom
field for lookups, so we also re-run the field setup to pick up the new
``unique=1`` constraint.
"""

import frappe


CUSTOMER_NAMING_SERIES = "CUST-.YYYY.-"


def execute():
    if not frappe.db.exists("DocType", "Medusa Setting"):
        return

    _set_customer_naming_by_series()
    _ensure_naming_series_option(CUSTOMER_NAMING_SERIES)

    from ecommerce_integrations.medusa.custom_fields import setup_custom_fields
    setup_custom_fields()


def _set_customer_naming_by_series():
    """Tell Selling Settings to autoname Customers via naming series."""
    if not frappe.db.exists("DocType", "Selling Settings"):
        return
    current = frappe.db.get_single_value("Selling Settings", "cust_master_name")
    if current == "Naming Series":
        return
    frappe.db.set_single_value("Selling Settings", "cust_master_name", "Naming Series")


def _ensure_naming_series_option(series: str):
    """Make sure the given series is part of Customer.naming_series options.

    We use a Property Setter so the option survives ERPNext upgrades.
    """
    if not frappe.db.exists("DocType", "Customer"):
        return

    meta = frappe.get_meta("Customer")
    field = meta.get_field("naming_series")
    if not field:
        return

    options = (field.options or "").splitlines()
    options = [o for o in (line.strip() for line in options) if o]
    if series in options:
        return
    options.insert(0, series)
    new_options = "\n".join(options)

    ps_name = frappe.db.get_value(
        "Property Setter",
        {"doc_type": "Customer", "field_name": "naming_series", "property": "options"},
    )
    if ps_name:
        frappe.db.set_value("Property Setter", ps_name, "value", new_options)
    else:
        frappe.get_doc({
            "doctype": "Property Setter",
            "doctype_or_field": "DocField",
            "doc_type": "Customer",
            "field_name": "naming_series",
            "property": "options",
            "property_type": "Text",
            "value": new_options,
        }).insert(ignore_permissions=True)
    frappe.clear_cache(doctype="Customer")
