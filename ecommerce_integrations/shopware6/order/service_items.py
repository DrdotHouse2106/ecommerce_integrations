"""
Shopware 6 Checkout Fields & Service Items Handler

Delegates to shared checkout_utils for common logic.
Adds Shopware-specific service item detection from line items.
"""

from typing import Any, Dict, Optional

from ecommerce_integrations.ecommerce_integrations.checkout_utils import (
    process_checkout_fields as _process_fields,
    add_service_item,
)


def process_checkout_fields(
    so: "frappe.Document",
    setting,
    order_data: Dict[str, Any],
    customer: Optional[str] = None,
) -> None:
    """Process checkout custom fields from Shopware order data."""
    from ecommerce_integrations.shopware6.order.order_mapper import extract_checkout_field_value

    service_items = _process_fields(
        so, setting, order_data, extract_checkout_field_value, customer=customer,
    )

    # Shopware-specific: detect service items present as Shopware line items
    if service_items:
        _detect_service_items_in_line_items(so, setting, order_data, service_items)


def _detect_service_items_in_line_items(
    so: "frappe.Document",
    setting,
    order_data: Dict[str, Any],
    service_items: Dict[str, Any],
) -> None:
    """Detect service items present as Shopware line items and add them."""
    for line_item in (order_data.get("lineItems") or []):
        product_number = line_item.get("payload", {}).get("productNumber", "")
        if not product_number:
            continue
        for item_code, row in service_items.items():
            if product_number == item_code:
                add_service_item(so, setting, item_code, row.service_price)
                break
