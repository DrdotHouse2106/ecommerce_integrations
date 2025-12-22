"""Utility functions for Shopware 6 Integration"""

import time
import functools
from typing import Any, Callable, Dict, Optional, Tuple, Type, Union

import frappe
from frappe import _

from ecommerce_integrations.shopware6.constants import (
    MODULE_NAME,
    LOG_DOCTYPE,
    SETTING_DOCTYPE,
)


# Gateway error codes that should trigger a retry
RETRIABLE_STATUS_CODES = (502, 503, 504)
RETRIABLE_ERROR_MESSAGES = (
    "Bad Gateway",
    "Service Unavailable",
    "Gateway Timeout",
    "502",
    "503",
    "504",
)


def is_retriable_error(error: Exception) -> bool:
    """
    Check if an error is a retriable gateway error.

    Args:
        error: The exception to check

    Returns:
        True if the error is a retriable gateway error
    """
    error_str = str(error)
    return any(msg in error_str for msg in RETRIABLE_ERROR_MESSAGES)


def retry_on_gateway_error(
    max_retries: int = 3,
    initial_delay: float = 2.0,
    backoff_factor: float = 2.0,
    max_delay: float = 30.0,
) -> Callable:
    """
    Decorator that retries a function on gateway errors (502, 503, 504).

    Uses exponential backoff between retries.

    Args:
        max_retries: Maximum number of retry attempts (default: 3)
        initial_delay: Initial delay in seconds before first retry (default: 2.0)
        backoff_factor: Multiplier for delay between retries (default: 2.0)
        max_delay: Maximum delay in seconds between retries (default: 30.0)

    Usage:
        @retry_on_gateway_error(max_retries=3)
        def my_api_call():
            return client.request_get('product')
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_exception = None
            delay = initial_delay

            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last_exception = e

                    # Check if this is a retriable error
                    if not is_retriable_error(e):
                        raise

                    # If we've exhausted retries, raise the last exception
                    if attempt >= max_retries:
                        frappe.log_error(
                            f"Gateway error after {max_retries} retries: {e}",
                            "Shopware Gateway Error - Max Retries Exceeded"
                        )
                        raise

                    # Log the retry attempt
                    frappe.log_error(
                        f"Gateway error (attempt {attempt + 1}/{max_retries + 1}): {e}\n"
                        f"Retrying in {delay:.1f} seconds...",
                        "Shopware Gateway Error - Retrying"
                    )

                    # Wait before retrying
                    time.sleep(delay)

                    # Increase delay for next retry (exponential backoff)
                    delay = min(delay * backoff_factor, max_delay)

            # Should not reach here, but just in case
            if last_exception:
                raise last_exception

        return wrapper
    return decorator


def execute_with_retry(
    func: Callable,
    args: tuple = (),
    kwargs: dict = None,
    max_retries: int = 3,
    initial_delay: float = 2.0,
    backoff_factor: float = 2.0,
    max_delay: float = 30.0,
) -> Any:
    """
    Execute a function with retry logic for gateway errors.

    This is a non-decorator version for cases where you can't use a decorator.

    Args:
        func: The function to execute
        args: Positional arguments to pass to the function
        kwargs: Keyword arguments to pass to the function
        max_retries: Maximum number of retry attempts
        initial_delay: Initial delay in seconds before first retry
        backoff_factor: Multiplier for delay between retries
        max_delay: Maximum delay in seconds between retries

    Returns:
        The return value of the function

    Raises:
        The last exception if all retries fail
    """
    if kwargs is None:
        kwargs = {}

    last_exception = None
    delay = initial_delay

    for attempt in range(max_retries + 1):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            last_exception = e

            # Check if this is a retriable error
            if not is_retriable_error(e):
                raise

            # If we've exhausted retries, raise the last exception
            if attempt >= max_retries:
                frappe.log_error(
                    f"Gateway error after {max_retries} retries: {e}",
                    "Shopware Gateway Error - Max Retries Exceeded"
                )
                raise

            # Log the retry attempt
            frappe.log_error(
                f"Gateway error (attempt {attempt + 1}/{max_retries + 1}): {e}\n"
                f"Retrying in {delay:.1f} seconds...",
                "Shopware Gateway Error - Retrying"
            )

            # Wait before retrying
            time.sleep(delay)

            # Increase delay for next retry (exponential backoff)
            delay = min(delay * backoff_factor, max_delay)

    # Should not reach here, but just in case
    if last_exception:
        raise last_exception


def create_shopware_log(
    status: str = "Queued",
    request_data: Optional[Dict[str, Any]] = None,
    response_data: Optional[Dict[str, Any]] = None,
    message: Optional[str] = None,
    exception: Optional[str] = None,
    method: Optional[str] = None,
    rollback: bool = False,
) -> "frappe.Document":
    """
    Create a log entry for Shopware sync operations.

    Args:
        status: Log status (Queued, Success, Error, Skipped)
        request_data: Request payload sent to Shopware
        response_data: Response received from Shopware
        message: Additional message/notes
        exception: Exception traceback if any
        method: The method that created this log
        rollback: Whether to rollback transaction before logging

    Returns:
        The created Shopware Log document
    """
    if rollback:
        frappe.db.rollback()

    log = frappe.new_doc(LOG_DOCTYPE)
    log.status = status
    log.method = method
    log.message = message

    if request_data:
        log.request_data = frappe.as_json(request_data, indent=2)

    if response_data:
        log.response_data = frappe.as_json(response_data, indent=2)

    if exception:
        # Wrap in repr() to ensure it's valid Python code (string literal)
        # because the 'exception' field is of type Code with options=Python
        log.exception = repr(str(exception))

    try:
        log.insert(ignore_permissions=True)
        if rollback:
            frappe.db.commit()
    except Exception as e:
        frappe.log_error(f"Failed to create Shopware log: {e}")

    return log


def update_shopware_log(
    log_name: str,
    status: Optional[str] = None,
    response_data: Optional[Dict[str, Any]] = None,
    message: Optional[str] = None,
    exception: Optional[str] = None,
):
    """
    Update an existing Shopware log entry.

    Args:
        log_name: Name of the log document to update
        status: New status
        response_data: Response data to add
        message: Message to add/update
        exception: Exception to add
    """
    try:
        updates = {}
        if status:
            updates["status"] = status
        if response_data:
            updates["response_data"] = frappe.as_json(response_data, indent=2)
        if message:
            updates["message"] = message
        if exception:
            # Wrap in repr() for consistency, although set_value might bypass validation
            updates["exception"] = repr(str(exception))

        if updates:
            frappe.db.set_value(LOG_DOCTYPE, log_name, updates)
    except Exception as e:
        frappe.log_error(f"Failed to update Shopware log {log_name}: {e}")


def get_shopware_setting():
    """Get the Shopware Setting document."""
    return frappe.get_doc(SETTING_DOCTYPE)


def is_shopware_enabled() -> bool:
    """Check if Shopware integration is enabled."""
    try:
        setting = get_shopware_setting()
        return setting.is_enabled()
    except Exception:
        return False


def get_shopware_document_id(doctype: str, docname: str) -> Optional[str]:
    """
    Get the Shopware ID for an ERPNext document.

    Args:
        doctype: ERPNext DocType (e.g., 'Item', 'Customer')
        docname: ERPNext document name

    Returns:
        Shopware ID if synced, None otherwise
    """
    ecommerce_item = frappe.db.get_value(
        "Ecommerce Item",
        {"integration": MODULE_NAME, "erpnext_item_code": docname},
        "integration_item_code",
    )
    return ecommerce_item


def format_shopware_datetime(dt_string: str) -> Optional[str]:
    """
    Convert Shopware datetime format to ERPNext format.

    Shopware uses ISO 8601 format: 2024-01-15T10:30:00.000+00:00
    ERPNext uses: 2024-01-15 10:30:00

    Args:
        dt_string: Shopware datetime string

    Returns:
        ERPNext formatted datetime string
    """
    if not dt_string:
        return None

    from datetime import datetime

    try:
        # Parse ISO 8601 format
        if "T" in dt_string:
            # Remove timezone info for parsing
            dt_str = dt_string.split("+")[0].split("Z")[0]
            dt = datetime.fromisoformat(dt_str)
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        return dt_string
    except Exception:
        return dt_string


def format_erpnext_datetime(dt) -> str:
    """
    Convert ERPNext datetime to Shopware ISO 8601 format.

    Args:
        dt: ERPNext datetime object or string

    Returns:
        ISO 8601 formatted datetime string
    """
    from datetime import datetime

    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt.replace(" ", "T"))

    return dt.strftime("%Y-%m-%dT%H:%M:%S.000+00:00")


def clean_shopware_id(shopware_id: str) -> str:
    """
    Clean and validate a Shopware UUID.

    Shopware uses 32-character hex UUIDs without dashes.

    Args:
        shopware_id: Raw Shopware ID

    Returns:
        Cleaned ID (lowercase, no dashes)
    """
    if not shopware_id:
        return ""
    return shopware_id.lower().replace("-", "")


def get_price_from_shopware_price_object(price_obj: Dict[str, Any], return_net: bool = False) -> float:
    """
    Extract price from a Shopware price object.

    Shopware price objects have a complex structure with currency-specific prices.
    Can return either gross or net price.

    Args:
        price_obj: Shopware price object
        return_net: If True, return net price; otherwise return gross price

    Returns:
        Price as float (gross or net depending on return_net parameter)
    """
    if not price_obj:
        return 0.0

    # Handle list of prices (multiple currencies)
    if isinstance(price_obj, list) and price_obj:
        price_obj = price_obj[0]

    # Direct price value (no gross/net distinction)
    if isinstance(price_obj, (int, float)):
        return float(price_obj)

    # Nested structure with gross/net
    if return_net:
        net = price_obj.get("net", 0)
        if net:
            return float(net)
        # Fallback: calculate from gross if net not available
        gross = price_obj.get("gross", 0)
        if gross:
            # Default German VAT 19%
            return float(gross) / 1.19
    else:
        gross = price_obj.get("gross", 0)
        if gross:
            return float(gross)

    # Calculated prices structure
    calculated = price_obj.get("calculatedPrice", {})
    if return_net:
        net_price = calculated.get("netPrice", 0)
        if net_price:
            return float(net_price)
        # Fallback: calculate from totalPrice
        total_price = calculated.get("totalPrice", 0)
        if total_price:
            return float(total_price) / 1.19
    return float(calculated.get("totalPrice", 0))


def convert_gross_to_net(gross_price: float, tax_rate: float = 19.0) -> float:
    """
    Convert a gross price to net price by removing the tax.

    Formula: net = gross / (1 + tax_rate/100)

    Args:
        gross_price: Price including tax
        tax_rate: Tax rate as percentage (e.g., 19 for 19%)

    Returns:
        Net price (excluding tax)
    """
    if gross_price <= 0 or tax_rate < 0:
        return gross_price
    return round(gross_price / (1 + tax_rate / 100), 2)


def convert_net_to_gross(net_price: float, tax_rate: float = 19.0) -> float:
    """
    Convert a net price to gross price by adding the tax.

    Formula: gross = net * (1 + tax_rate/100)

    Args:
        net_price: Price excluding tax
        tax_rate: Tax rate as percentage (e.g., 19 for 19%)

    Returns:
        Gross price (including tax)
    """
    if net_price <= 0 or tax_rate < 0:
        return net_price
    return round(net_price * (1 + tax_rate / 100), 2)


def get_tax_rate_from_line_item(line_item: Dict[str, Any], default_rate: float = 19.0) -> float:
    """
    Extract the tax rate from a Shopware line item.

    Shopware stores calculated taxes in line_item.price.calculatedTaxes.

    Args:
        line_item: Shopware order line item
        default_rate: Default tax rate if not found

    Returns:
        Tax rate as percentage (e.g., 19.0 for 19%)
    """
    price = line_item.get("price", {})
    calculated_taxes = price.get("calculatedTaxes", [])

    if calculated_taxes and len(calculated_taxes) > 0:
        # Get the first tax rate (most items have single tax)
        return float(calculated_taxes[0].get("taxRate", default_rate))

    return default_rate


def get_net_unit_price_from_line_item(line_item: Dict[str, Any], default_tax_rate: float = 19.0) -> float:
    """
    Extract or calculate the net unit price from a Shopware line item.

    Shopware's unitPrice is typically gross. This function calculates
    the net price using the tax rate from the item.

    Args:
        line_item: Shopware order line item
        default_tax_rate: Default tax rate if not found in item

    Returns:
        Net unit price
    """
    unit_price = float(line_item.get("unitPrice", 0))

    if unit_price <= 0:
        return 0.0

    # Get tax rate from item
    tax_rate = get_tax_rate_from_line_item(line_item, default_tax_rate)

    # Convert gross to net
    return convert_gross_to_net(unit_price, tax_rate)


def map_country_code(shopware_country_iso: str) -> Optional[str]:
    """
    Map Shopware country ISO code to ERPNext country name.

    Args:
        shopware_country_iso: Two-letter ISO country code

    Returns:
        ERPNext country name
    """
    if not shopware_country_iso:
        return None

    country = frappe.db.get_value("Country", {"code": shopware_country_iso.lower()}, "name")
    return country


def map_state_from_shopware(country_state_data: Dict[str, Any], country_name: str) -> Optional[str]:
    """
    Map Shopware countryState to ERPNext state name.
    
    Shopware countryState has:
    - shortCode: e.g. "DE-NW" for Nordrhein-Westfalen
    - name: Display name e.g. "Nordrhein-Westfalen"
    
    Args:
        country_state_data: Shopware countryState object
        country_name: ERPNext country name (e.g. "Germany")
    
    Returns:
        ERPNext state name if found
    """
    if not country_state_data or not isinstance(country_state_data, dict):
        return None
    
    # Get the state code (e.g., "DE-NW" -> "NW") or name
    short_code = country_state_data.get("shortCode", "")
    state_name = country_state_data.get("name", "") or country_state_data.get("translated", {}).get("name", "")
    
    # Extract state code from shortCode (e.g., "DE-NW" -> "NW")
    if short_code and "-" in short_code:
        state_code = short_code.split("-")[-1]
    else:
        state_code = short_code
    
    # Try to find state in ERPNext by code first
    if state_code and country_name:
        state = frappe.db.get_value(
            "State",
            {"country": country_name, "state_code": state_code.upper()},
            "name"
        )
        if state:
            return state
    
    # Try to find by name
    if state_name and country_name:
        state = frappe.db.get_value(
            "State",
            {"country": country_name, "state_name": state_name},
            "name"
        )
        if state:
            return state
        
        # Also try just matching by name field
        state = frappe.db.get_value(
            "State",
            {"country": country_name, "name": ["like", f"%{state_name}%"]},
            "name"
        )
        if state:
            return state
    
    # Last resort: return the display name as-is if states exist for this country
    if state_name:
        return state_name
    
    return None


def generate_item_code_from_shopware(product: Dict[str, Any]) -> str:
    """
    Generate an item code for a Shopware product.

    Uses product number if available, otherwise falls back to ID.

    Args:
        product: Shopware product data

    Returns:
        Item code string
    """
    product_number = product.get("productNumber", "")
    if product_number:
        return product_number

    return product.get("id", "")[:20]
