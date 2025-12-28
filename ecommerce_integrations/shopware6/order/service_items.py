"""
Shopware 6 Service Items Handler

Handles service items like Tel. Avis and Forklift/Hebebühne.
"""

from typing import Any, Dict

import frappe
from frappe.utils import flt

from ecommerce_integrations.shopware6.constants import SERVICE_PRODUCTS


def add_service_items_if_needed(
    so: "frappe.Document",
    setting,
    order_data: Dict[str, Any],
    tel_avis_requested: bool = False,
    forklift_requested: bool = False
) -> None:
    """
    Add service items (Tel. Avis, Forklift/Hebebühne) to the Sales Order if requested.

    The services can come from:
    1. A custom field checkbox in the order
    2. An actual line item with the service product number

    Args:
        so: Sales Order document
        setting: Shopware Setting document
        order_data: Shopware order data
        tel_avis_requested: Whether tel avis was requested via custom field
        forklift_requested: Whether forklift was requested via custom field
    """
    line_items = order_data.get("lineItems", [])

    # Get all product numbers already in the order
    existing_product_numbers = set()
    for line_item in line_items:
        product_number = line_item.get("payload", {}).get("productNumber", "")
        if product_number:
            existing_product_numbers.add(product_number)

    # Handle Tel. Avis service
    if tel_avis_requested and getattr(setting, 'enable_tel_avis_service', True):
        _add_tel_avis_item(so, setting, existing_product_numbers)

    # Handle Forklift/Hebebühne service
    if forklift_requested and getattr(setting, 'enable_forklift_service', True):
        _add_forklift_item(so, setting, existing_product_numbers)


def _add_tel_avis_item(
    so: "frappe.Document",
    setting,
    existing_product_numbers: set
) -> None:
    """
    Add Tel. Avis service item to Sales Order.

    Args:
        so: Sales Order document
        setting: Shopware Setting document
        existing_product_numbers: Set of product numbers already in the order
    """
    tel_avis_item_code = (
        getattr(setting, 'tel_avis_item', None) or
        SERVICE_PRODUCTS.get("tel_avis", "SERVICE-TEL-AVIS")
    )

    if tel_avis_item_code in existing_product_numbers:
        return

    # Check if the item exists in ERPNext
    if frappe.db.exists("Item", tel_avis_item_code):
        item_rate = frappe.db.get_value("Item", tel_avis_item_code, "standard_rate")
        if item_rate is None:
            item_rate = getattr(setting, 'tel_avis_price', 7.50) or 7.50
        so.append(
            "items",
            {
                "item_code": tel_avis_item_code,
                "qty": 1,
                "rate": flt(item_rate),
                "warehouse": setting.warehouse,
                "delivery_date": so.delivery_date,
                "description": "Service: Telefonisches Avis",
            },
        )
    else:
        # Item doesn't exist - log a warning so the admin knows to create it
        frappe.log_error(
            f"Tel. Avis item '{tel_avis_item_code}' not found in ERPNext. Please create this item.",
            "Shopware Order Sync - Missing Service Item"
        )


def _add_forklift_item(
    so: "frappe.Document",
    setting,
    existing_product_numbers: set
) -> None:
    """
    Add Forklift/Hebebühne service item to Sales Order.

    Args:
        so: Sales Order document
        setting: Shopware Setting document
        existing_product_numbers: Set of product numbers already in the order
    """
    forklift_item_code = (
        getattr(setting, 'forklift_item', None) or
        SERVICE_PRODUCTS.get("forklift", "SERVICE-FORKLIFT")
    )

    if forklift_item_code in existing_product_numbers:
        return

    # Check if the item exists in ERPNext
    if frappe.db.exists("Item", forklift_item_code):
        item_rate = frappe.db.get_value("Item", forklift_item_code, "standard_rate")
        if item_rate is None:
            item_rate = getattr(setting, 'forklift_price', 0) or 0
        so.append(
            "items",
            {
                "item_code": forklift_item_code,
                "qty": 1,
                "rate": flt(item_rate),
                "warehouse": setting.warehouse,
                "delivery_date": so.delivery_date,
                "description": "Service: Hebebühne / Forklift",
            },
        )
    else:
        # Item doesn't exist - log a warning so the admin knows to create it
        frappe.log_error(
            f"Forklift item '{forklift_item_code}' not found in ERPNext. Please create this item.",
            "Shopware Order Sync - Missing Service Item"
        )


def add_service_item_if_needed(
    so: "frappe.Document",
    setting,
    order_data: Dict[str, Any],
    tel_avis_requested: bool = False
) -> None:
    """
    Backwards compatible wrapper for add_service_items_if_needed.

    Only handles Tel. Avis for backwards compatibility.
    Use add_service_items_if_needed for full functionality.
    """
    add_service_items_if_needed(so, setting, order_data, tel_avis_requested, False)


def detect_service_items_from_line_items(
    line_items: list,
    setting
) -> tuple:
    """
    Detect if service items are present in line items.

    Args:
        line_items: List of Shopware line items
        setting: Shopware Setting document

    Returns:
        tuple: (tel_avis_detected, forklift_detected)
    """
    tel_avis_detected = False
    forklift_detected = False

    tel_avis_item = (
        getattr(setting, 'tel_avis_item', None) or
        SERVICE_PRODUCTS.get("tel_avis", "SERVICE-TEL-AVIS")
    )
    forklift_item = (
        getattr(setting, 'forklift_item', None) or
        SERVICE_PRODUCTS.get("forklift", "SERVICE-FORKLIFT")
    )

    for line_item in line_items:
        product_number = line_item.get("payload", {}).get("productNumber", "") if line_item.get("payload") else ""

        # Check for Tel. Avis service product
        if not tel_avis_detected:
            if product_number == tel_avis_item or "tel" in product_number.lower() or "avis" in product_number.lower():
                tel_avis_detected = True

        # Check for Forklift/Hebebühne service product
        if not forklift_detected:
            if product_number == forklift_item or "forklift" in product_number.lower() or "hebebuehne" in product_number.lower() or "hebebühne" in product_number.lower():
                forklift_detected = True

        if tel_avis_detected and forklift_detected:
            break

    return tel_avis_detected, forklift_detected
