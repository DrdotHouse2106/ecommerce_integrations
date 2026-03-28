"""
Shopware 6 Checkout Fields & Service Items Handler

Processes checkout custom fields configured in Shopware Settings.
Supports mapping to Sales Order fields, Customer fields, and service items.
"""

from typing import Any, Dict, Optional

import frappe
from frappe.utils import flt

from ecommerce_integrations.shopware6.utils import get_logger


def process_checkout_fields(
    so: "frappe.Document",
    setting,
    order_data: Dict[str, Any],
    customer: Optional[str] = None,
) -> None:
    """
    Process all configured checkout custom fields from Shopware order data.

    Reads the checkout_fields table from Shopware Settings and processes each
    enabled mapping: sets Sales Order fields, updates Customer records, or
    adds service items.

    Args:
        so: Sales Order document (being built, not yet saved)
        setting: Shopware Setting document
        order_data: Shopware order data dict
        customer: ERPNext Customer name (for Customer Update mappings)
    """
    from ecommerce_integrations.shopware6.order.order_mapper import extract_checkout_field_value

    so_meta = frappe.get_meta("Sales Order")
    service_items = {}

    for row in (setting.get("checkout_fields") or []):
        if not row.enabled:
            continue

        field_names = [n.strip() for n in (row.shopware_field_names or "").split(",") if n.strip()]
        value = extract_checkout_field_value(order_data, field_names)

        # Collect service items for line-item detection below
        if row.mapping_type == "Service Item" and row.service_item:
            service_items[row.service_item] = row
            if value and bool(value):
                _add_service_item(so, setting, row.service_item, row.service_price)

        elif value is None:
            continue

        elif row.mapping_type == "Sales Order Field" and row.target_field:
            if so_meta.has_field(row.target_field):
                setattr(so, row.target_field, value)

        elif row.mapping_type == "Customer Update" and row.target_field and customer:
            if frappe.db.has_column("Customer", row.target_field):
                frappe.db.set_value(
                    "Customer", customer, row.target_field, value,
                    update_modified=False
                )

    # Also detect service items present as Shopware line items
    if service_items:
        _detect_service_items_in_line_items(so, setting, order_data, service_items)


def _add_service_item(
    so: "frappe.Document",
    setting,
    item_code: str,
    override_price: float = None,
) -> None:
    """
    Add a service item to the Sales Order.

    Args:
        so: Sales Order document
        setting: Shopware Setting document
        item_code: ERPNext Item code
        override_price: Optional price override
    """
    # Check if already added
    for item in (so.get("items") or []):
        if item.item_code == item_code:
            return

    rate = override_price
    if rate is None:
        standard_rate = frappe.db.get_value("Item", item_code, "standard_rate")
        if standard_rate is None:
            get_logger().warning(
                f"Service item '{item_code}' not found in ERPNext. "
                f"Create this item or update Shopware Settings."
            )
            return
        rate = standard_rate or 0

    so.append("items", {
        "item_code": item_code,
        "qty": 1,
        "rate": flt(rate),
        "warehouse": setting.warehouse,
        "delivery_date": so.delivery_date,
    })


def _detect_service_items_in_line_items(
    so: "frappe.Document",
    setting,
    order_data: Dict[str, Any],
    service_items: Dict[str, Any],
) -> None:
    """
    Detect service items present as Shopware line items and add them.

    Args:
        service_items: Dict of {item_code: checkout_field_row} built by caller
    """
    for line_item in (order_data.get("lineItems") or []):
        product_number = line_item.get("payload", {}).get("productNumber", "")
        if not product_number:
            continue
        for item_code, row in service_items.items():
            if product_number == item_code:
                _add_service_item(so, setting, item_code, row.service_price)
                break
