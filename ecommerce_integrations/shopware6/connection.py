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
import uuid
from collections.abc import Callable
from typing import Any

import frappe
from frappe import _

# Import Shopware SDK
from lib_shopware6_api_base import (
    ConfShopware6ApiBase,
    ContainsFilter,
    Criteria,
    EqualsFilter,
    MultiFilter,
    RangeFilter,
    Shopware6AdminAPIClientBase,
)
from lib_shopware6_api_base.conf_shopware6_api_base_classes import ShopwareAPIError


def criteria_to_dict(criteria: Criteria) -> dict:
    """
    Convert a Criteria Pydantic model to a clean dict for the Shopware API.

    The newer lib_shopware6_api_base versions use Pydantic BaseModel for Criteria,
    which serializes empty lists (ids: [], aggregations: [], etc.) by default.
    Shopware 6 API rejects requests with empty 'ids' arrays.

    This function strips empty/null fields recursively, but preserves
    empty dicts inside 'associations' (Shopware uses {} to mean "load this association").
    """
    data = criteria.model_dump(mode="json")
    return _strip_empty(data)


def _strip_empty(obj, _parent_key: str = ""):
    """Recursively remove None values and empty lists/dicts from a dict.

    Empty dicts inside 'associations' are preserved because Shopware API
    uses {"lineItems": {}} to request loading of that association.
    """
    if isinstance(obj, dict):
        cleaned = {}
        for k, v in obj.items():
            v = _strip_empty(v, _parent_key=k)
            if v is None:
                continue
            if isinstance(v, list) and not v:
                continue
            # Keep empty dicts inside 'associations' - Shopware needs them
            if isinstance(v, dict) and not v and _parent_key != "associations":
                continue
            cleaned[k] = v
        return cleaned
    elif isinstance(obj, list):
        return [_strip_empty(item) for item in obj if item is not None]
    return obj

from ecommerce_integrations.shopware6.constants import (
    SETTING_DOCTYPE,
)
from ecommerce_integrations.shopware6.utils import (
    get_logger,
    is_retriable_error,
    require_shopware_admin,
)

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

    client = Shopware6AdminAPIClientBase(config=config)
    _patch_client_timeout(client, timeout=60)
    _patch_client_binary_upload(client)
    _patch_client_criteria(client)
    return client


def _patch_client_binary_upload(client: Shopware6AdminAPIClientBase):
    """
    Fix binary file uploads in lib_shopware6_api_base.

    The library's _request() method does `request_data = str(payload)` for bytes,
    which produces the Python repr string b'\\x89PNG...' instead of raw bytes.
    This patch intercepts POST requests with bytes payload and sends them correctly
    using httpx's `content` parameter with raw bytes.
    """
    import httpx
    from lib_shopware6_api_base.lib_shopware6_admin_client import HttpMethod

    _orig_request = client._request

    def _request_with_binary_fix(
        http_method, request_url, payload=None, content_type="json",
        additional_query_params=None, update_header_fields=None,
    ):
        # Only intercept POST with bytes payload (binary upload)
        if http_method == HttpMethod.POST and isinstance(payload, bytes):
            # Ensure session is authenticated
            client._get_session()

            headers = client._get_headers(
                content_type=content_type,
                update_header_fields=update_header_fields,
            )
            url = client._format_admin_api_url(request_url)

            response = client.session.post(
                url,
                content=payload,  # Raw bytes, not str(payload)
                headers=headers,
                params=additional_query_params or {},
                follow_redirects=client.config.follow_redirects,
            )

            # Same error handling as the library
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                detailed_error = f" : {exc.response.text}"
                raise ShopwareAPIError(f"{exc}{detailed_error}") from exc

            return response

        # All other requests go through the original path
        return _orig_request(
            http_method, request_url, payload,
            content_type=content_type,
            additional_query_params=additional_query_params,
            update_header_fields=update_header_fields,
        )

    client._request = _request_with_binary_fix


def _patch_client_criteria(client: Shopware6AdminAPIClientBase):
    """
    Patch request_post to auto-convert Criteria Pydantic models to clean dicts.

    The newer lib_shopware6_api_base uses Pydantic BaseModel for Criteria,
    which serializes empty lists (ids: [], etc.). Shopware 6 rejects empty 'ids'.
    This patch transparently cleans Criteria payloads before sending.
    """
    _orig_request_post = client.request_post

    def _request_post_with_clean_criteria(request_url, payload=None, **kwargs):
        if payload is not None and isinstance(payload, Criteria):
            payload = criteria_to_dict(payload)
        return _orig_request_post(request_url, payload, **kwargs)

    client.request_post = _request_post_with_clean_criteria


def _patch_client_timeout(client: Shopware6AdminAPIClientBase, timeout: int = 60):
    """
    Patch the Shopware client to enforce HTTP timeouts and better error handling.

    The library's session.post()/get() calls have NO timeout by default,
    causing requests to hang indefinitely. We patch _get_session() so that
    every session (including refreshed ones) gets a timeout on the client.

    Also wraps token acquisition to produce clear error messages when the
    Shopware API returns empty responses (e.g. reverse proxy blocking).

    httpx 0.28+ removed the timeout kwarg from send(), so we set it
    directly on the client/session object via its timeout property.
    """
    import httpx

    _original_get_session = client._get_session

    def _get_session_with_timeout():
        try:
            _original_get_session()
        except json.JSONDecodeError as e:
            raise ShopwareAPIError(
                f"Shopware API returned empty/invalid response during authentication. "
                f"This usually means the shop URL is unreachable or blocked by a reverse proxy. "
                f"Configured URL: {client.config.shopware_admin_api_url} — Error: {e}"
            ) from e
        # Set timeout on the (possibly new) session object
        if not getattr(client.session, '_timeout_patched', False):
            client.session.timeout = httpx.Timeout(timeout)
            client.session._timeout_patched = True

    client._get_session = _get_session_with_timeout


def _attach_idempotency_key(client: Shopware6AdminAPIClientBase, key: str) -> None:
    """Inject an ``sw-api-idempotency-key`` header into every request the
    client makes.

    Shopware honours this header on write endpoints (``POST /_action/sync``,
    ``POST /category``, …) — a 504 after a successful server-side write means
    the retry sees the same key and Shopware short-circuits instead of
    replaying the write. The same key is reused across all retries of the
    SAME call (we generate it once per ``temp_shopware_session`` invocation).
    """
    try:
        # The base client builds headers in _get_headers; intercepting it lets
        # us add our header on every flavour of request (GET/POST/PATCH/DELETE).
        _orig_get_headers = client._get_headers

        def _get_headers_with_idempotency(*args, **kwargs):
            headers = _orig_get_headers(*args, **kwargs)
            try:
                headers["sw-api-idempotency-key"] = key
            except Exception:
                pass
            return headers

        client._get_headers = _get_headers_with_idempotency
    except Exception:
        # Header injection is best-effort. If the SDK internal changed shape,
        # we still want the request to go out.
        pass


def temp_shopware_session(
    func: Callable | None = None,
    *,
    max_retries: int = DEFAULT_MAX_RETRIES,
    initial_delay: float = DEFAULT_INITIAL_DELAY,
    backoff_factor: float = DEFAULT_BACKOFF_FACTOR,
    max_delay: float = DEFAULT_MAX_DELAY,
    retry_writes: bool = True,
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
        retry_writes: If True (default), retried calls reuse the same
            ``sw-api-idempotency-key`` so Shopware can detect a successful
            server-side write whose response was lost (504/timeout) and avoid
            replaying it. Set to False on fail-fast write paths where the
            caller does its own idempotency (e.g. external job-level dedup).

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

            # One idempotency key per decorated-function invocation. Reused
            # across all retries of THIS call so Shopware can short-circuit
            # a replayed write whose original response was lost. A retried
            # call from a different worker / different invocation gets its
            # own fresh key — different write, different key.
            idempotency_key = uuid.uuid4().hex if retry_writes else None
            if idempotency_key:
                _attach_idempotency_key(client, idempotency_key)

            last_exception = None
            delay = initial_delay

            for attempt in range(max_retries + 1):
                try:
                    return fn(client, *args, **kwargs)
                except (Exception, ShopwareAPIError) as e:
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

                    # Get a fresh client for retry (in case of connection issues),
                    # but reuse the SAME idempotency key so Shopware can detect
                    # a replay of a write whose response we lost.
                    client = get_shopware_client()
                    if idempotency_key:
                        _attach_idempotency_key(client, idempotency_key)

            # Should not reach here, but just in case
            if last_exception:
                raise last_exception

        return wrapper

    # Support both @temp_shopware_session and @temp_shopware_session()
    if func is not None:
        return decorator(func)
    return decorator


def test_connection() -> dict[str, Any]:
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
def test_shopware_connection() -> dict[str, Any]:
    """Whitelisted method to test connection from the frontend.

    Also writes a single ``Ecommerce Integration Log`` row stamped with
    method ``test_connection`` so the Setting form's health banner
    (``shopware6.api.setup.get_health``) can prove "tested recently"
    without relying on a dedicated timestamp field.
    """
    require_shopware_admin()
    result = test_connection()
    try:
        from ecommerce_integrations.shopware6.utils import create_shopware_log

        create_shopware_log(
            status="Success" if result.get("success") else "Error",
            method="test_connection",
            message=result.get("message"),
            response_data={
                "sales_channels": result.get("sales_channels"),
                "success": result.get("success"),
            },
            make_new=True,
        )
    except Exception:
        # Logging is best-effort — never fail a connection test because
        # the audit row couldn't be written.
        pass
    return result


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
            from ecommerce_integrations.shopware6.utils import create_shopware_log, get_logger
            create_shopware_log(
                method="webhook_handler",
                request_data={"payload_length": len(frappe.request.data or b"")},
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
            request_data={"payload_length": len(frappe.request.data or b"")}
        )
        raise


def process_webhook(event_type: str, data: dict[str, Any]) -> None:
    """
    Process webhook based on event type.

    Args:
        event_type: Shopware event type (e.g., 'order.placed', 'product.written')
        data: Webhook payload data

    Dedup strategy: every incoming webhook is funnelled through
    :class:`WebhookEventQueue`, which holds a Redis lock per entity and a 60s
    "already processed" hash. The dedup key is the ``sw-context-message-id``
    request header — Shopware sets it once per webhook fire and keeps it stable
    across retries, whereas the payload's ``updatedAt`` fields drift on retry
    and would defeat a payload-hash-based key.
    """
    from ecommerce_integrations.shopware6.utils import create_shopware_log

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

    if not handler:
        create_shopware_log(
            method="webhook_handler",
            request_data=data,
            status="Skipped",
            message=f"Unknown event type: {event_type}",
        )
        return

    # Pull dedup key from the request header (set by Shopware once per fire,
    # stable across retries). Fall back to a synthesised key.
    message_id: str | None = None
    if frappe.request is not None:
        try:
            message_id = frappe.request.headers.get("sw-context-message-id")
        except Exception:
            message_id = None

    # Pick out a stable entity_id for the per-entity lock. Different Shopware
    # event payload shapes carry the id in different places.
    entity_id = (
        data.get("id")
        or (data.get("data") or {}).get("id")
        or ((data.get("payload") or [{}])[0].get("primaryKey") if isinstance(data.get("payload"), list) else None)
        or "unknown"
    )

    timestamp = data.get("timestamp") or data.get("createdAt")
    fallback_event_id = message_id or f"{event_type}:{entity_id}:{timestamp or ''}"

    # Use the dedup primitives directly (not WebhookEventQueue.enqueue, which
    # would process synchronously inside the HTTP request). We want to dedup
    # at enqueue-time and dispatch to the worker pool.
    from ecommerce_integrations.shopware6.webhook.event_queue import WebhookEventQueue

    queue = WebhookEventQueue()
    if queue._is_duplicate(str(entity_id), data, timestamp, event_id=fallback_event_id):
        # Already saw this exact event recently — drop on the floor with a log.
        create_shopware_log(
            method="webhook_handler",
            request_data=data,
            status="Skipped",
            message=f"Duplicate webhook (event_id={fallback_event_id})",
        )
        return

    # Create log entry, then enqueue the real handler.
    log = create_shopware_log(
        method=handler,
        request_data=data,
        status="Queued",
        message=f"event_id={fallback_event_id}",
    )

    frappe.enqueue(
        method=handler,
        queue="short",
        timeout=300,
        is_async=True,
        payload=data,
        request_id=log.name,
    )


# Re-export commonly used SDK classes for convenience
__all__ = [
    "ConfShopware6ApiBase",
    "ContainsFilter",
    "Criteria",
    "EqualsFilter",
    "MultiFilter",
    "RangeFilter",
    "Shopware6AdminAPIClientBase",
    "ShopwareAPIError",
    "get_shopware_client",
    "temp_shopware_session",
    "test_connection",
    "test_shopware_connection",
    "webhook_handler",
]
