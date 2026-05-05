"""
Shopware 6 Order Mapper

Maps and extracts data from Shopware order payloads.
"""

from typing import Any
import re

import frappe
from frappe.utils import flt, getdate, add_days

from ecommerce_integrations.shopware6.constants import (
    PAYMENT_METHOD_MAP,
    PAYMENT_STATE_MAP,
    DEFAULT_MODE_OF_PAYMENT,
    EU_COUNTRY_CODES,
    DOMESTIC_COUNTRY_CODE,
    TAX_TEMPLATE_MAP,
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


def extract_checkout_field_value(order_data: dict, field_names: list) -> Any:
    """
    Extract a custom field value from Shopware order data by searching
    multiple locations for any of the given field names.

    Searches in order: order.customFields, orderCustomer.customFields,
    then direct order payload fields (webhook format).

    Args:
        order_data: Shopware order object
        field_names: List of Shopware field name variants to search for

    Returns:
        Field value or None if not found
    """
    custom_fields = order_data.get("customFields", {}) or {}
    for name in field_names:
        if name in custom_fields:
            return custom_fields[name]

    customer_cf = (order_data.get("orderCustomer", {}) or {}).get("customFields", {}) or {}
    for name in field_names:
        if name in customer_cf:
            return customer_cf[name]

    for name in field_names:
        if name in order_data:
            return order_data[name]

    return None


def _normalize_payment_method(name: str) -> str:
    return (name or "").strip().lower().replace("-", "_").replace(" ", "_")


def _resolve_default_mode_of_payment(setting=None):
    if setting is not None:
        configured = getattr(setting, "default_mode_of_payment", None)
        if configured and frappe.db.exists("Mode of Payment", configured):
            return configured
    if frappe.db.exists("Mode of Payment", DEFAULT_MODE_OF_PAYMENT):
        return DEFAULT_MODE_OF_PAYMENT
    return None


def _resolve_mode_of_payment(payment_method_name: str, setting=None):
    """Resolve a Shopware payment method to a valid ERPNext Mode of Payment.

    Lookup: Setting table (exact, then partial) → hardcoded ``PAYMENT_METHOD_MAP``
    (exact, then partial) → ``Setting.default_mode_of_payment`` →
    ``DEFAULT_MODE_OF_PAYMENT``. The resolved value is only returned if it
    exists on the site; otherwise ``None`` so we don't write a broken Link.
    """
    normalized = _normalize_payment_method(payment_method_name)
    candidate = None

    rows = list(getattr(setting, "payment_method_mappings", None) or []) if setting else []

    if normalized:
        for row in rows:
            if _normalize_payment_method(row.shopware_method) == normalized:
                candidate = row.mode_of_payment
                break
        if not candidate:
            for row in rows:
                row_key = _normalize_payment_method(row.shopware_method)
                if row_key and (row_key in normalized or normalized in row_key):
                    candidate = row.mode_of_payment
                    break
        if not candidate and normalized in PAYMENT_METHOD_MAP:
            candidate = PAYMENT_METHOD_MAP[normalized]
        if not candidate:
            for key, value in PAYMENT_METHOD_MAP.items():
                if key in normalized:
                    candidate = value
                    break

    if candidate and frappe.db.exists("Mode of Payment", candidate):
        return candidate

    return _resolve_default_mode_of_payment(setting)


def get_payment_method_info(order_data: dict, setting=None) -> tuple:
    """
    Extract payment method and status from Shopware order.

    Args:
        order_data: Shopware order object
        setting: Loaded ``Shopware Setting`` doc (for the configurable mapping
            and default Mode of Payment). Optional — falls back to hardcoded
            defaults when not provided.

    Returns:
        tuple: (payment_method_name, erpnext_mode_of_payment, payment_status)
    """
    transactions = order_data.get("transactions", []) or []

    if not transactions:
        return None, _resolve_default_mode_of_payment(setting), "Unpaid"

    # Prefer newest transaction because older transactions can remain in history
    # with obsolete states (e.g. failed/cancelled before retry).
    transactions_sorted = sorted(
        transactions,
        key=lambda t: (t.get("createdAt") or t.get("updatedAt") or ""),
        reverse=True,
    )
    transaction = transactions_sorted[0]
    if not (transaction.get("stateMachineState") or {}).get("technicalName"):
        transaction = next(
            (
                t for t in transactions_sorted
                if (t.get("stateMachineState") or {}).get("technicalName")
            ),
            transaction,
        )

    payment_method = transaction.get("paymentMethod", {}) or {}
    payment_method_name = payment_method.get("shortName", "") or payment_method.get("name", "")

    erpnext_mode = _resolve_mode_of_payment(payment_method_name, setting)

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


def get_delivery_country_code(order_data: dict) -> str:
    """
    Extract delivery country ISO code from Shopware order.

    Checks deliveries -> shippingOrderAddress -> country first,
    then falls back to billingAddress -> country.

    Args:
        order_data: Shopware order object

    Returns:
        Country ISO code (e.g., "DE", "AT", "CH") or "DE" as default
    """
    # Try delivery address first
    deliveries = order_data.get("deliveries", []) or []
    for delivery in deliveries:
        shipping_address = delivery.get("shippingOrderAddress", {}) or {}
        country = shipping_address.get("country", {}) or {}
        iso_code = country.get("iso", "") or country.get("isoCode", "")
        if iso_code:
            return iso_code.upper()

    # Fallback to billing address
    billing_address = order_data.get("billingAddress", {}) or {}
    country = billing_address.get("country", {}) or {}
    iso_code = country.get("iso", "") or country.get("isoCode", "")
    if iso_code:
        return iso_code.upper()

    # Default to domestic
    return DOMESTIC_COUNTRY_CODE


def has_valid_vat_id(order_data: dict) -> bool:
    """
    Check if the order has a valid VAT ID (B2B customer).

    Checks orderCustomer -> vatIds first, then customer -> vatIds.

    Args:
        order_data: Shopware order object

    Returns:
        True if VAT ID is present and non-empty
    """
    # Check orderCustomer vatIds
    order_customer = order_data.get("orderCustomer", {}) or {}
    vat_ids = order_customer.get("vatIds", []) or []
    if vat_ids and any(v for v in vat_ids if v):
        return True

    # Check nested customer object
    customer = order_customer.get("customer", {}) or {}
    vat_ids = customer.get("vatIds", []) or []
    if vat_ids and any(v for v in vat_ids if v):
        return True

    return False


def get_tax_template(order_data: dict, company: str) -> str:
    """
    Determine the correct ERPNext tax template based on delivery country and B2B status.

    Logic:
    - Domestic (DE): Use standard inland template
    - EU + VAT ID (B2B): Use EU B2B template (reverse charge, 0%)
    - EU without VAT ID (B2C): Use EU B2C template (German VAT)
    - Non-EU (Drittland): Use export template (0%)

    Args:
        order_data: Shopware order object with deliveries and orderCustomer
        company: ERPNext company name

    Returns:
        Tax template name or empty string if not found
    """
    # Get company abbreviation for template name
    company_abbr = frappe.db.get_value("Company", company, "abbr") or "KG"

    # Determine delivery country
    country_code = get_delivery_country_code(order_data)

    # Determine B2B status
    is_b2b = has_valid_vat_id(order_data)

    # Select appropriate template key
    if country_code == DOMESTIC_COUNTRY_CODE:
        template_key = "domestic"
    elif country_code in EU_COUNTRY_CODES:
        template_key = "eu_b2b" if is_b2b else "eu_b2c"
    else:
        template_key = "export"

    # Get template name pattern
    template_pattern = TAX_TEMPLATE_MAP.get(template_key, "")
    if not template_pattern:
        return ""

    # Format with company abbreviation
    template_name = template_pattern.format(company_abbr=company_abbr)

    # Verify template exists
    if frappe.db.exists("Sales Taxes and Charges Template", template_name):
        return template_name

    # Log warning if template not found
    frappe.logger("shopware6").warning(
        f"Tax template '{template_name}' not found for {country_code}/{template_key}"
    )

    return ""
