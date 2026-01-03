"""Utility functions for Shopware 6 Integration"""

import time
import functools
from typing import Any, Callable, Dict, Optional, Tuple, Type, Union

import frappe
from frappe import _

from ecommerce_integrations.ecommerce_integrations.doctype.ecommerce_integration_log.ecommerce_integration_log import (
    create_log,
)
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
                        logger = get_logger("retry_on_gateway_error")
                        logger.error(
                            f"Gateway error after {max_retries} retries",
                            exception=e,
                            persist=True
                        )
                        raise

                    # Log the retry attempt
                    frappe.logger().warning(
                        f"Gateway error (attempt {attempt + 1}/{max_retries + 1}): {e}. "
                        f"Retrying in {delay:.1f} seconds..."
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
                logger = get_logger("execute_with_retry")
                logger.error(
                    f"Gateway error after {max_retries} retries",
                    exception=e,
                    persist=True
                )
                raise

            # Log the retry attempt
            frappe.logger().warning(
                f"Gateway error (attempt {attempt + 1}/{max_retries + 1}): {e}. "
                f"Retrying in {delay:.1f} seconds..."
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
    make_new: bool = False,
) -> "frappe.Document":
    """
    Create a log entry for Shopware sync operations using shared Ecommerce Integration Log.
    
    This function now uses the shared logging infrastructure from ecommerce_integration_log
    to maintain consistency with other integrations (Shopify, Unicommerce).

    Args:
        status: Log status (Queued, Success, Error, Skipped)
        request_data: Request payload sent to Shopware
        response_data: Response received from Shopware
        message: Additional message/notes
        exception: Exception traceback if any
        method: The method that created this log
        rollback: Whether to rollback transaction before logging
        make_new: Force creation of new log entry

    Returns:
        The created Ecommerce Integration Log document
    """
    return create_log(
        module_def=MODULE_NAME,
        status=status,
        request_data=request_data,
        response_data=response_data,
        message=message,
        exception=exception,
        method=method,
        rollback=rollback,
        make_new=make_new,
    )


def update_shopware_log(
    log_name: str,
    status: Optional[str] = None,
    response_data: Optional[Dict[str, Any]] = None,
    message: Optional[str] = None,
    exception: Optional[str] = None,
):
    """
    Update an existing Shopware log entry (Ecommerce Integration Log).

    Args:
        log_name: Name of the log document to update
        status: New status
        response_data: Response data to add
        message: Message to add/update
        exception: Exception to add
    """
    try:
        log = frappe.get_doc("Ecommerce Integration Log", log_name)
        
        if status:
            log.status = status
        if response_data:
            log.response_data = frappe.as_json(response_data, indent=2)
        if message:
            log.message = message
        if exception:
            log.traceback = frappe.get_traceback() if not isinstance(exception, str) else exception
            
        log.save(ignore_permissions=True)
        frappe.db.commit()
    except Exception as e:
        # Fallback to frappe logger if log update fails
        frappe.logger().error(f"Failed to update Shopware log {log_name}: {e}")


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


def get_tax_rate_from_shopware_product(product_data: Dict[str, Any]) -> float:
    """
    Extract the tax rate from a Shopware product.

    Shopware stores tax information in the 'tax' association which contains
    the taxRate field. This function extracts it for accurate price calculations.

    Args:
        product_data: Shopware product object with tax association

    Returns:
        Tax rate as float (e.g., 19.0 for 19%), defaults to 19.0 if not found
    """
    if not product_data:
        return 19.0  # German default

    # Try direct tax association
    tax = product_data.get("tax", {})
    if tax and isinstance(tax, dict):
        tax_rate = tax.get("taxRate")
        if tax_rate is not None:
            return float(tax_rate)

    # Try nested in price object
    prices = product_data.get("price", []) or product_data.get("prices", [])
    if prices and isinstance(prices, list) and prices:
        price_entry = prices[0]
        # Some price entries have tax info
        tax_rules = price_entry.get("taxRules", [])
        if tax_rules and isinstance(tax_rules, list) and tax_rules:
            first_rule = tax_rules[0]
            if first_rule.get("taxRate"):
                return float(first_rule["taxRate"])

    # Try calculatedPrice structure
    calculated = product_data.get("calculatedPrice", {})
    if calculated:
        calculated_tax = calculated.get("calculatedTaxes", [])
        if calculated_tax and isinstance(calculated_tax, list) and calculated_tax:
            return float(calculated_tax[0].get("taxRate", 19.0))

    return 19.0  # Default German VAT


def get_price_from_shopware_price_object(
    price_obj: Dict[str, Any],
    return_net: bool = False,
    tax_rate: Optional[float] = None
) -> float:
    """
    Extract price from a Shopware price object.

    Shopware price objects have a complex structure with currency-specific prices.
    Can return either gross or net price.

    Args:
        price_obj: Shopware price object
        return_net: If True, return net price; otherwise return gross price
        tax_rate: Tax rate for gross/net conversion (defaults to 19.0 if not provided)

    Returns:
        Price as float (gross or net depending on return_net parameter)
    """
    if not price_obj:
        return 0.0

    # Use provided tax rate or default to German VAT
    effective_tax_rate = tax_rate if tax_rate is not None else 19.0
    tax_divisor = 1 + (effective_tax_rate / 100)

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
            return round(float(gross) / tax_divisor, 2)
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
            return round(float(total_price) / tax_divisor, 2)
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
    state_name = country_state_data.get("name", "") or (country_state_data.get("translated") or {}).get("name", "")
    
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


class ShopwareLogger:
    """
    Unified logging for Shopware 6 integration.

    Consolidates all logging patterns:
    - frappe.log_error() -> error() with persist=True
    - frappe.logger("shopware6") -> info(), warning(), debug()
    - create_shopware_log() -> success(), error() with structured data

    Usage:
        logger = ShopwareLogger("sync_product")
        logger.info("Starting sync")
        logger.success("Product synced", request_data={"item": "ABC"})
        logger.error("Sync failed", exception=e)
    """

    def __init__(self, method: str = None):
        """
        Initialize logger with optional method name.

        Args:
            method: The method/operation name for tracking in logs
        """
        self.method = method
        self._logger = frappe.logger("shopware6")

    def debug(self, message: str):
        """
        Log debug message (console only, not persisted).

        Args:
            message: Debug message
        """
        self._logger.debug(f"[{self.method}] {message}" if self.method else message)

    def info(self, message: str, persist: bool = False, request_data: Dict[str, Any] = None):
        """
        Log info message, optionally persist to Shopware Log.

        Args:
            message: Info message
            persist: If True, create Shopware Log entry
            request_data: Optional request data to log
        """
        self._logger.info(f"[{self.method}] {message}" if self.method else message)
        if persist:
            create_shopware_log(
                status="Info",
                message=message,
                method=self.method,
                request_data=request_data,
            )

    def warning(self, message: str, persist: bool = False, request_data: Dict[str, Any] = None):
        """
        Log warning message, optionally persist to Shopware Log.

        Args:
            message: Warning message
            persist: If True, create Shopware Log entry
            request_data: Optional request data to log
        """
        self._logger.warning(f"[{self.method}] {message}" if self.method else message)
        if persist:
            create_shopware_log(
                status="Warning",
                message=message,
                method=self.method,
                request_data=request_data,
            )

    def error(
        self,
        message: str,
        exception: Exception = None,
        persist: bool = True,
        request_data: Dict[str, Any] = None,
        response_data: Dict[str, Any] = None,
        rollback: bool = False,
    ):
        """
        Log error message, persists to Shopware Log by default.

        Args:
            message: Error message
            exception: Optional exception object
            persist: If True (default), create Shopware Log entry
            request_data: Optional request data to log
            response_data: Optional response data to log
            rollback: If True, rollback DB transaction before logging
        """
        full_message = f"[{self.method}] {message}" if self.method else message
        self._logger.error(full_message)

        if persist:
            create_shopware_log(
                status="Error",
                message=message,
                exception=str(exception) if exception else None,
                method=self.method,
                request_data=request_data,
                response_data=response_data,
                rollback=rollback,
            )

    def success(
        self,
        message: str,
        request_data: Dict[str, Any] = None,
        response_data: Dict[str, Any] = None,
    ):
        """
        Log successful operation to Shopware Log.

        Args:
            message: Success message
            request_data: Optional request data to log
            response_data: Optional response data to log
        """
        self._logger.info(f"[{self.method}] SUCCESS: {message}" if self.method else f"SUCCESS: {message}")
        create_shopware_log(
            status="Success",
            message=message,
            method=self.method,
            request_data=request_data,
            response_data=response_data,
        )

    def queued(
        self,
        message: str,
        request_data: Dict[str, Any] = None,
    ) -> "frappe.Document":
        """
        Log queued operation to Shopware Log and return the log document.

        Useful for tracking async operations.

        Args:
            message: Queue message
            request_data: Optional request data to log

        Returns:
            The created Shopware Log document for later updates
        """
        self._logger.debug(f"[{self.method}] QUEUED: {message}" if self.method else f"QUEUED: {message}")
        return create_shopware_log(
            status="Queued",
            message=message,
            method=self.method,
            request_data=request_data,
        )

    def update_log(
        self,
        log_name: str,
        status: str = None,
        message: str = None,
        response_data: Dict[str, Any] = None,
        exception: Exception = None,
    ):
        """
        Update an existing Shopware Log entry.

        Args:
            log_name: Name of the log document to update
            status: New status
            message: New message
            response_data: Response data to add
            exception: Exception to add
        """
        update_shopware_log(
            log_name=log_name,
            status=status,
            message=message,
            response_data=response_data,
            exception=str(exception) if exception else None,
        )


def get_logger(method: str = None) -> ShopwareLogger:
    """
    Get a ShopwareLogger instance.

    Convenience function for getting a logger.

    Args:
        method: The method/operation name for tracking in logs

    Returns:
        ShopwareLogger instance

    Usage:
        from ecommerce_integrations.shopware6.utils import get_logger

        logger = get_logger("sync_product")
        logger.info("Starting sync")
    """
    return ShopwareLogger(method)
