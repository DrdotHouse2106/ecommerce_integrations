"""
Shopware 6 API Connection Module

Uses lib_shopware6_api_base SDK for OAuth2 authentication and API communication.
Provides a decorator for temporary sessions similar to the Shopify integration.
Includes automatic retry logic for gateway errors (502, 503, 504).
"""

import functools
import hashlib
import hmac
import json
import time
from typing import Any, Callable, Dict, Optional

import frappe
from frappe import _

# Import Shopware SDK
from lib_shopware6_api_base import (
    Shopware6AdminAPIClientBase,
    ConfShopware6ApiBase,
    Criteria,
    EqualsFilter,
    RangeFilter,
    ContainsFilter,
    MultiFilter,
)
from lib_shopware6_api_base.conf_shopware6_api_base_classes import ShopwareAPIError

from ecommerce_integrations.shopware6.constants import (
    SETTING_DOCTYPE,
    MODULE_NAME,
)
from ecommerce_integrations.shopware6.utils import get_logger, is_retriable_error


# Retry configuration for gateway errors
DEFAULT_MAX_RETRIES = 3
DEFAULT_INITIAL_DELAY = 2.0
DEFAULT_BACKOFF_FACTOR = 2.0
DEFAULT_MAX_DELAY = 30.0


def get_shopware_client() -> Shopware6AdminAPIClientBase:
    """
    Create and return a configured Shopware 6 Admin API client.

    Uses the credentials stored in Shopware Setting DocType.
    Supports both 'resource_owner' (Integration) and 'user_credentials' grant types.

    Returns:
        Shopware6AdminAPIClientBase: Configured API client with OAuth2 authentication

    Raises:
        frappe.ValidationError: If Shopware integration is not enabled or configured
    """
    setting = frappe.get_doc(SETTING_DOCTYPE)

    if not setting.enable_shopware:
        frappe.throw(_("Shopware 6 integration is not enabled."))

    # Build the API URL from shop URL
    shop_url = setting.shop_url.rstrip("/")
    if not shop_url.startswith("http"):
        shop_url = f"https://{shop_url}"

    admin_api_url = f"{shop_url}/api"

    # Create configuration based on grant type
    config = ConfShopware6ApiBase(
        shopware_admin_api_url=admin_api_url,
        grant_type=setting.grant_type or "resource_owner",
    )

    if setting.grant_type == "user_credentials":
        # User credentials (password) grant type
        config.username = setting.api_username
        config.password = setting.get_password("api_password")
    else:
        # Resource owner (Integration/Client credentials) grant type - default
        config.client_id = setting.client_id
        config.client_secret = setting.get_password("client_secret")

    return Shopware6AdminAPIClientBase(config=config)


def temp_shopware_session(
    func: Callable = None,
    *,
    max_retries: int = DEFAULT_MAX_RETRIES,
    initial_delay: float = DEFAULT_INITIAL_DELAY,
    backoff_factor: float = DEFAULT_BACKOFF_FACTOR,
    max_delay: float = DEFAULT_MAX_DELAY,
) -> Callable:
    """
    Decorator that provides a Shopware API client to the decorated function.

    The client is passed as the first argument to the function.
    Handles authentication and session management automatically.
    Includes automatic retry with exponential backoff for gateway errors (502, 503, 504).

    Args:
        func: The function to decorate (automatically passed when used without parentheses)
        max_retries: Maximum number of retry attempts for gateway errors (default: 3)
        initial_delay: Initial delay in seconds before first retry (default: 2.0)
        backoff_factor: Multiplier for delay between retries (default: 2.0)
        max_delay: Maximum delay in seconds between retries (default: 30.0)

    Usage:
        @temp_shopware_session
        def my_api_function(client: Shopware6AdminAPIClientBase, *args, **kwargs):
            response = client.request_get('product')
            return response

        # Or with custom retry settings:
        @temp_shopware_session(max_retries=5, initial_delay=5.0)
        def my_critical_api_function(client, *args, **kwargs):
            ...

    Note: In test mode (frappe.flags.in_test), the function is called without
    the client argument to allow mocking.
    """
    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            # Skip auth in testing mode
            if frappe.flags.in_test:
                return fn(*args, **kwargs)

            setting = frappe.get_doc(SETTING_DOCTYPE)
            if not setting.is_enabled():
                # If not enabled, silently return (like Shopify does)
                # This prevents errors when the integration is disabled but hooks are still registered
                return None

            client = get_shopware_client()
            last_exception = None
            delay = initial_delay

            for attempt in range(max_retries + 1):
                try:
                    return fn(client, *args, **kwargs)
                except Exception as e:
                    last_exception = e

                    # Check if this is a retriable gateway error
                    if not is_retriable_error(e):
                        raise

                    # If we've exhausted retries, raise the last exception
                    if attempt >= max_retries:
                        logger = get_logger("retry_on_gateway_errors")
                        logger.error(f"Gateway error after {max_retries} retries in {fn.__name__}", exception=e, persist=True)
                        raise

                    # Log the retry attempt (as info, not error, to avoid log spam)
                    frappe.logger().warning(
                        f"Shopware gateway error in {fn.__name__} (attempt {attempt + 1}/{max_retries + 1}): {e}. "
                        f"Retrying in {delay:.1f}s..."
                    )

                    # Wait before retrying with exponential backoff
                    time.sleep(delay)
                    delay = min(delay * backoff_factor, max_delay)

                    # Get a fresh client for retry (in case of connection issues)
                    client = get_shopware_client()

            # Should not reach here, but just in case
            if last_exception:
                raise last_exception

        return wrapper

    # Support both @temp_shopware_session and @temp_shopware_session()
    if func is not None:
        return decorator(func)
    return decorator


def test_connection() -> Dict[str, Any]:
    """
    Test the Shopware API connection.

    Returns:
        dict: API response with shop info or error details

    Raises:
        ShopwareAPIError: If connection fails
    """
    try:
        client = get_shopware_client()
        # Try to get basic info - sales channel is a good test endpoint
        response = client.request_get("sales-channel")
        return {
            "success": True,
            "message": _("Connection successful!"),
            "sales_channels": len(response.get("data", [])),
        }
    except ShopwareAPIError as e:
        return {
            "success": False,
            "message": str(e),
        }
    except Exception as e:
        return {
            "success": False,
            "message": _("Connection failed: {0}").format(str(e)),
        }


@frappe.whitelist()
def test_shopware_connection() -> Dict[str, Any]:
    """Whitelisted method to test connection from the frontend."""
    return test_connection()


def validate_webhook_signature(payload: bytes, signature: str, secret: str) -> bool:
    """
    Validate incoming webhook signature from Shopware.

    Shopware 6 uses HMAC-SHA256 for webhook signatures.

    Args:
        payload: Raw request body bytes
        signature: Signature from X-Shopware-Signature header
        secret: Webhook secret configured in Shopware

    Returns:
        bool: True if signature is valid
    """
    if not secret or not signature:
        return False

    computed_signature = hmac.new(
        secret.encode("utf-8"),
        payload,
        hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(computed_signature, signature)


@frappe.whitelist(allow_guest=True)
def webhook_handler():
    """
    Handle incoming webhooks from Shopware 6.

    Shopware sends webhooks for various events like order creation,
    product updates, etc. This endpoint validates and processes them.

    SECURITY: Webhook signature validation is MANDATORY when webhook_secret is configured.
    If no secret is configured, webhooks are rejected by default unless
    explicitly allowed via allow_unsigned_webhooks setting.
    """
    if not frappe.request:
        return

    try:
        setting = frappe.get_doc(SETTING_DOCTYPE)
        if not setting.is_enabled():
            frappe.throw(_("Shopware integration is not enabled"))

        # Get signature from headers
        signature = frappe.get_request_header("X-Shopware-Signature") or ""

        # SECURITY: Enforce webhook signature validation
        webhook_secret = setting.get_password("webhook_secret") if setting.webhook_secret else None

        if webhook_secret:
            # Secret configured - validate signature (MANDATORY)
            if not validate_webhook_signature(
                frappe.request.data,
                signature,
                webhook_secret
            ):
                frappe.throw(_("Invalid webhook signature"), frappe.AuthenticationError)
        else:
            # No secret configured - check if unsigned webhooks are explicitly allowed
            allow_unsigned = getattr(setting, "allow_unsigned_webhooks", False)
            if not allow_unsigned:
                frappe.logger("shopware6").warning(
                    "SECURITY WARNING: Webhook received without signature validation. "
                    "Configure webhook_secret in Shopware Settings or enable allow_unsigned_webhooks."
                )
                frappe.throw(
                    _("Webhook signature validation required. Configure webhook_secret in Shopware Settings."),
                    frappe.AuthenticationError
                )

        # Parse payload with error handling
        try:
            data = json.loads(frappe.request.data)
        except json.JSONDecodeError as e:
            from ecommerce_integrations.shopware6.utils import get_logger, create_shopware_log
            create_shopware_log(
                method="webhook_handler",
                request_data={"raw": frappe.request.data.decode("utf-8", errors="replace")[:1000]},
                status="Error",
                message=f"Invalid JSON in webhook payload: {e}"
            )
            frappe.throw(_("Invalid webhook payload format"))

        event_type = data.get("event", "unknown")

        # Process webhook based on event type
        process_webhook(event_type, data)

        return {"success": True}

    except Exception as e:
        logger = get_logger("handle_webhook")
        logger.error(
            f"Webhook processing error: {e}",
            exception=e,
            persist=True,
            request_data={"payload": frappe.request.data}
        )
        raise


def process_webhook(event_type: str, data: Dict[str, Any]) -> None:
    """
    Process webhook based on event type.

    Args:
        event_type: Shopware event type (e.g., 'order.placed', 'product.written')
        data: Webhook payload data
    """
    from ecommerce_integrations.shopware6.utils import get_logger, create_shopware_log

    # Map event types to handler methods
    EVENT_HANDLERS = {
        "order.placed": "ecommerce_integrations.shopware6.order.sync_order_from_webhook",
        "order.updated": "ecommerce_integrations.shopware6.order.sync_order_from_webhook",  # Same handler, will update if custom fields changed
        "order.state.changed": "ecommerce_integrations.shopware6.order.update_order_status",
        "order_transaction.state.changed": "ecommerce_integrations.shopware6.payment.handle_transaction_state_change",
        "product.written": "ecommerce_integrations.shopware6.product.sync_product_from_webhook",
        "customer.written": "ecommerce_integrations.shopware6.customer.sync_customer_from_webhook",
    }

    handler = EVENT_HANDLERS.get(event_type)

    if handler:
        # Create log entry
        log = create_shopware_log(
            method=handler,
            request_data=data,
            status="Queued"
        )

        # Enqueue background job
        frappe.enqueue(
            method=handler,
            queue="short",
            timeout=300,
            is_async=True,
            payload=data,
            request_id=log.name,
        )
    else:
        # Log unknown event type
        create_shopware_log(
            method="webhook_handler",
            request_data=data,
            status="Skipped",
            message=f"Unknown event type: {event_type}"
        )


# Re-export commonly used SDK classes for convenience
__all__ = [
    "get_shopware_client",
    "temp_shopware_session",
    "test_connection",
    "test_shopware_connection",
    "webhook_handler",
    "Shopware6AdminAPIClientBase",
    "ConfShopware6ApiBase",
    "Criteria",
    "EqualsFilter",
    "RangeFilter",
    "ContainsFilter",
    "MultiFilter",
    "ShopwareAPIError",
]
