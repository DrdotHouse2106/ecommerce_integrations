"""
Shopware 6 Order Mapper

Maps and extracts data from Shopware order payloads.
"""

from typing import Any
import re

import frappe
from frappe.utils import flt, getdate, add_days

from ecommerce_integrations.shopware6.constants import (
    SHOPWARE_CHECKOUT_CUSTOM_FIELDS,
    PAYMENT_METHOD_MAP,
    PAYMENT_STATE_MAP,
    DEFAULT_MODE_OF_PAYMENT,
)
from ecommerce_integrations.shopware6.product import get_item_code


def parse_delivery_time(delivery_time_str: str) -> int:
    """
    Parse a delivery time string to extract the maximum number of days.

    Examples:
        "3-5 Werktage" -> 5
        "ca. 2 Wochen" -> 14
        "1-2 Wochen" -> 14
        "5 Tage" -> 5
        "lieferbar in 10 Tagen" -> 10

    Args:
        delivery_time_str: Delivery time string from Item.delivery_time

    Returns:
        Maximum delivery days as integer (default: 1)
    """
    if not delivery_time_str:
        return 1

    delivery_time_str = delivery_time_str.lower()

    # Extract all numbers from the string
    numbers = re.findall(r'\d+', delivery_time_str)
    if not numbers:
        return 1

    # Get the maximum number found
    max_value = max(int(n) for n in numbers)

    # Check if the unit is weeks (German: Woche/Wochen)
    if 'woche' in delivery_time_str:
        max_value *= 7
    # Check for months (German: Monat/Monate)
    elif 'monat' in delivery_time_str:
        max_value *= 30

    return max_value


def calculate_delivery_date(order_date: str, line_items: list) -> str:
    """
    Calculate delivery date based on the longest delivery time of items in the order.

    Reads the `delivery_time` field from each Item (e.g., "3-5 Werktage")
    and parses the maximum value. Falls back to `lead_time_days` if
    `delivery_time` is not set.

    Args:
        order_date: Order date in YYYY-MM-DD format or datetime
        line_items: List of Shopware line items from the order

    Returns:
        Delivery date as string (YYYY-MM-DD format)
    """
    # Default minimum lead time is 1 day (no same-day delivery)
    max_lead_time = 1

    for line_item in line_items:
        # Get item code from line item
        item_code = get_item_code(line_item)
        if not item_code:
            continue

        # Get delivery_time (text field like "3-5 Werktage") and lead_time_days (fallback)
        item_data = frappe.db.get_value(
            "Item",
            item_code,
            ["delivery_time", "lead_time_days"],
            as_dict=True
        )

        if not item_data:
            continue

        # Prefer delivery_time field, fallback to lead_time_days
        if item_data.get("delivery_time"):
            lead_time = parse_delivery_time(item_data.delivery_time)
        elif item_data.get("lead_time_days"):
            lead_time = int(item_data.lead_time_days)
        else:
            continue

        if lead_time > max_lead_time:
            max_lead_time = lead_time

    # Calculate delivery date: order_date + max_lead_time
    delivery_date = add_days(getdate(order_date), max_lead_time)
    return str(delivery_date)


def get_checkout_custom_field(order_data: dict, field_type: str) -> Any:
    """
    Extract a custom field value from Shopware order data.

    Shopware custom fields can be in multiple locations:
    - order.customFields
    - order.orderCustomer.customFields
    - Direct order payload fields (for webhook payloads)

    Args:
        order_data: Shopware order object
        field_type: Type of field to extract (po_number, tel_avis, forklift, invoice_email)

    Returns:
        Field value or None if not found
    """
    field_names = SHOPWARE_CHECKOUT_CUSTOM_FIELDS.get(field_type, [])

    # Check order customFields
    custom_fields = order_data.get("customFields", {}) or {}
    for field_name in field_names:
        if field_name in custom_fields:
            return custom_fields[field_name]

    # Check orderCustomer customFields
    order_customer = order_data.get("orderCustomer", {}) or {}
    customer_custom_fields = order_customer.get("customFields", {}) or {}
    for field_name in field_names:
        if field_name in customer_custom_fields:
            return customer_custom_fields[field_name]

    # Check direct payload fields (for webhook format)
    for field_name in field_names:
        if field_name in order_data:
            return order_data[field_name]

    return None


def get_payment_method_info(order_data: dict) -> tuple:
    """
    Extract payment method and status from Shopware order.

    Args:
        order_data: Shopware order object

    Returns:
        tuple: (payment_method_name, erpnext_mode_of_payment, payment_status)
    """
    transactions = order_data.get("transactions", []) or []

    if not transactions:
        return None, DEFAULT_MODE_OF_PAYMENT, "Unpaid"

    # Get the latest/first transaction
    transaction = transactions[0]

    # Get payment method
    payment_method = transaction.get("paymentMethod", {}) or {}
    payment_method_name = payment_method.get("shortName", "") or payment_method.get("name", "")

    # Map to ERPNext mode of payment
    erpnext_mode = DEFAULT_MODE_OF_PAYMENT
    payment_method_lower = payment_method_name.lower().replace("-", "_").replace(" ", "_")

    # Try direct mapping
    if payment_method_lower in PAYMENT_METHOD_MAP:
        erpnext_mode = PAYMENT_METHOD_MAP[payment_method_lower]
    else:
        # Try partial matching for known payment providers
        for key, value in PAYMENT_METHOD_MAP.items():
            if key in payment_method_lower:
                erpnext_mode = value
                break

    # Get payment status
    state = transaction.get("stateMachineState", {}) or {}
    state_name = state.get("technicalName", "open")
    payment_status = PAYMENT_STATE_MAP.get(state_name, "Unpaid")

    return payment_method_name, erpnext_mode, payment_status


def extract_order_currency(order_data: dict) -> str:
    """
    Extract currency code from Shopware order.

    Args:
        order_data: Shopware order object

    Returns:
        Currency ISO code (default: EUR)
    """
    currency = order_data.get("currency", {})
    return currency.get("isoCode", "EUR") if isinstance(currency, dict) else "EUR"


def get_tax_rate_from_line_item(line_item: dict, default_rate: float = 19.0) -> float:
    """
    Extract tax rate from a Shopware line item.

    Args:
        line_item: Shopware line item object
        default_rate: Default tax rate if not found

    Returns:
        Tax rate as float
    """
    price = line_item.get("price", {})
    calculated_taxes = price.get("calculatedTaxes", [])

    if calculated_taxes:
        return flt(calculated_taxes[0].get("taxRate", default_rate))

    return default_rate
