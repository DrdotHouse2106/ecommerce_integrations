"""Shared checkout field processing for all ecommerce integrations.

Both Shopware and Medusa use the same Ecommerce Checkout Field child table.
This module provides the common logic for applying those mappings during
order sync, parameterized by a value-extraction callback.
"""

from typing import Any, Callable, Dict, List, Optional

import frappe
from frappe.utils import flt

from ecommerce_integrations.ecommerce_integrations.ecommerce_custom_fields import (
    MAPPING_CUSTOMER_UPDATE,
    MAPPING_SALES_ORDER_FIELD,
    MAPPING_SERVICE_ITEM,
)


def process_checkout_fields(
    so,
    setting,
    source_data: Dict[str, Any],
    extract_value_fn: Callable[[Dict, List[str]], Any],
    customer: Optional[str] = None,
):
    """Process checkout field mappings from integration settings.

    Args:
        so: Sales Order — either a frappe Document or a plain dict.
        setting: Integration Setting document (Shopware Setting or Medusa Setting).
        source_data: Raw order data from the ecommerce platform.
        extract_value_fn: Callable(source_data, field_names) -> value.
            Platform-specific function to extract a checkout field value
            from the order data (e.g., Shopware customFields or Medusa metadata).
        customer: ERPNext Customer name (for "Customer Update" mappings).
    """
    so_meta = frappe.get_meta("Sales Order")
    customer_meta = frappe.get_meta("Customer") if customer else None
    is_doc = hasattr(so, "meta")

    service_items = {}
    customer_updates = {}

    for row in setting.get("checkout_fields") or []:
        if not row.enabled:
            continue

        field_names = [n.strip() for n in (row.source_field_names or "").split(",") if n.strip()]
        value = extract_value_fn(source_data, field_names)

        if row.mapping_type == MAPPING_SERVICE_ITEM and row.service_item:
            service_items[row.service_item] = row
            if value:
                add_service_item(so, setting, row.service_item, row.service_price, is_doc=is_doc)

        elif value is None:
            continue

        elif row.mapping_type == MAPPING_SALES_ORDER_FIELD and row.target_field:
            if so_meta.has_field(row.target_field):
                if is_doc:
                    setattr(so, row.target_field, value)
                else:
                    so[row.target_field] = value

        elif row.mapping_type == MAPPING_CUSTOMER_UPDATE and row.target_field and customer:
            if customer_meta and customer_meta.has_field(row.target_field):
                customer_updates[row.target_field] = value

    if customer_updates:
        frappe.db.set_value("Customer", customer, customer_updates, update_modified=False)

    return service_items


def add_service_item(so, setting, item_code: str, override_price=None, is_doc=True):
    """Add a service item to the Sales Order."""
    items = so.get("items") or []
    for item in items:
        code = item.item_code if is_doc else item.get("item_code")
        if code == item_code:
            return

    rate = override_price
    if rate is None:
        standard_rate = frappe.db.get_value("Item", item_code, "standard_rate")
        if standard_rate is None:
            frappe.log_error(
                f"Service item '{item_code}' not found in ERPNext. "
                "Create this item or update integration settings.",
                "Ecommerce Checkout Fields",
            )
            return
        rate = standard_rate or 0

    item_data = {
        "item_code": item_code,
        "qty": 1,
        "rate": flt(rate),
        "warehouse": setting.warehouse,
    }

    if is_doc:
        if hasattr(so, "delivery_date") and so.delivery_date:
            item_data["delivery_date"] = so.delivery_date
        so.append("items", item_data)
    else:
        so.setdefault("items", []).append(item_data)
