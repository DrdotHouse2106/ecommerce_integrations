"""Add AB-YYYY-NNNNN naming series for ecommerce sales orders.

Stops Medusa-imported orders from cluttering the SO-##### series and gives
each year its own counter. We:

1. Add ``AB-.YYYY.-`` as an option on Sales Order.naming_series via a Property
   Setter (so the option survives ERPNext upgrades).
2. Default Medusa Setting.sales_order_series to ``AB-.YYYY.-`` if it's still
   on the legacy ``SO-.#####`` value or unset.

Existing Sales Orders are left alone — only newly created orders pick up the
new series.
"""

import frappe


SALES_ORDER_SERIES = "AB-.YYYY.-"
LEGACY_SERIES = "SO-.#####"


def execute():
    if not frappe.db.exists("DocType", "Sales Order"):
        return

    _ensure_naming_series_option(SALES_ORDER_SERIES)
    _set_medusa_default_series(SALES_ORDER_SERIES)


def _ensure_naming_series_option(series: str):
    """Add the series to Sales Order.naming_series via Property Setter."""
    meta = frappe.get_meta("Sales Order")
    field = meta.get_field("naming_series")
    if not field:
        return

    options = [line.strip() for line in (field.options or "").splitlines() if line.strip()]
    if series in options:
        return
    options.insert(0, series)
    new_options = "\n".join(options)

    ps_name = frappe.db.get_value(
        "Property Setter",
        {"doc_type": "Sales Order", "field_name": "naming_series", "property": "options"},
    )
    if ps_name:
        frappe.db.set_value("Property Setter", ps_name, "value", new_options)
    else:
        frappe.get_doc({
            "doctype": "Property Setter",
            "doctype_or_field": "DocField",
            "doc_type": "Sales Order",
            "field_name": "naming_series",
            "property": "options",
            "property_type": "Text",
            "value": new_options,
        }).insert(ignore_permissions=True)
    frappe.clear_cache(doctype="Sales Order")


def _set_medusa_default_series(series: str):
    """Default the Medusa-imported Sales Order series to AB-YYYY-NNNNN."""
    if not frappe.db.exists("DocType", "Medusa Setting"):
        return
    current = frappe.db.get_single_value("Medusa Setting", "sales_order_series")
    if current and current != LEGACY_SERIES:
        return
    frappe.db.set_single_value("Medusa Setting", "sales_order_series", series)
