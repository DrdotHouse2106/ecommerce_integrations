"""Handle incoming events from the Medusa subscriber plugin.

Endpoint: /api/method/ecommerce_integrations.medusa.webhook_handler.handle_medusa_event

Expected payload (enriched form, sent by the Medusa subscriber):
{
    "event": "order.placed",
    "data": {
        "id": "order_01ABC...",
        "order": { ...full order with customer, items, addresses, sales_channel... }
    },
    "timestamp": "2026-03-28T12:00:00Z"
}

Customer events use the same shape with a "customer" key instead of "order".
The legacy form ({"id": "..."} only) is still accepted; handlers fall back to
fetching the entity via the Medusa Admin API when no embedded object is present.
"""

import hashlib
import hmac
import json

import frappe
from frappe import _

from ecommerce_integrations.medusa.constants import SETTING_DOCTYPE
from ecommerce_integrations.medusa.utils import create_medusa_log, is_medusa_enabled, update_medusa_log


@frappe.whitelist(allow_guest=True)
def handle_medusa_event():
    """Receive and process webhook events from Medusa subscriber plugin.

    Security model (mirrors Shopware's webhook handler):

    1. ``webhook_secret`` configured  -> HMAC-SHA256 signature is **mandatory**.
       Mismatch -> ``AuthenticationError``.
    2. No ``webhook_secret`` AND ``allow_unsigned_webhooks`` unset -> reject
       with ``AuthenticationError``. This is the safe default.
    3. No ``webhook_secret`` AND ``allow_unsigned_webhooks`` set -> accept,
       but log a warning. Only intended for local development.

    Dedup: a 5-minute Redis ``SET NX`` on ``sha256(raw_payload)`` drops exact
    repeats so a retry of a webhook we've already processed is a no-op.
    """
    if not is_medusa_enabled():
        frappe.throw(_("Medusa integration is not enabled"), frappe.AuthenticationError)

    payload = frappe.request.get_data(as_text=True)
    if not payload:
        frappe.throw(_("Empty payload"), frappe.ValidationError)

    setting = frappe.get_cached_doc(SETTING_DOCTYPE)
    webhook_secret = setting.get_password("webhook_secret") if setting.webhook_secret else None

    if webhook_secret:
        # Secret configured -> signature is mandatory.
        signature = frappe.request.headers.get("x-medusa-webhook-signature", "")
        expected = hmac.new(
            webhook_secret.encode("utf-8"),
            payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            frappe.throw(_("Invalid webhook signature"), frappe.AuthenticationError)
    else:
        # No secret -> only accept if explicitly opted in.
        allow_unsigned = bool(getattr(setting, "allow_unsigned_webhooks", False))
        if not allow_unsigned:
            frappe.logger("medusa").warning(
                "SECURITY: Medusa webhook received without signature validation. "
                "Configure webhook_secret in Medusa Setting, or enable "
                "allow_unsigned_webhooks for local development."
            )
            frappe.throw(
                _("Webhook signature validation required. Configure webhook_secret in Medusa Setting."),
                frappe.AuthenticationError,
            )

    # Dedup BEFORE parsing/JSON-decoding. The raw payload byte-stream is the
    # cleanest key — Medusa subscribers may resend the same body verbatim.
    event_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    dedup_key = f"medusa_wh_seen:{event_hash}"
    try:
        # frappe.cache wraps Redis; set(..., nx=True, ex=...) is SET NX EX.
        # Returns truthy if it was set (i.e. first time we see this), falsy
        # if the key already existed.
        first_time = frappe.cache.set(dedup_key, 1, nx=True, ex=300)
    except TypeError:
        # Older frappe.cache wrappers don't support nx/ex kwargs; fall back to
        # a get/set pair (slightly racy but still narrows the window).
        if frappe.cache.get_value(dedup_key):
            first_time = False
        else:
            frappe.cache.set_value(dedup_key, 1, expires_in_sec=300)
            first_time = True

    if not first_time:
        frappe.logger("medusa").info(
            f"Duplicate Medusa webhook dropped (sha256={event_hash[:12]}…)"
        )
        return

    data = json.loads(payload)
    event_type = data.get("event", "")
    event_data = data.get("data", {})

    log_name = create_medusa_log(
        request_type=f"Webhook: {event_type}",
        status="Queued",
        medusa_id=event_data.get("id", ""),
        request_data=data,
    )

    try:
        _route_event(event_type, event_data)
        update_medusa_log(log_name, status="Success")
    except Exception as e:
        update_medusa_log(log_name, status="Error", error=str(e))
        frappe.log_error(f"Medusa webhook error: {event_type}", str(e))


def _route_event(event_type: str, data: dict):
    """Route event to the appropriate handler via background queue."""
    handler_map = {
        "order.placed": "ecommerce_integrations.medusa.order.order_sync.sync_order",
        "order.updated": "ecommerce_integrations.medusa.order.order_sync.sync_order",
        "order.canceled": "ecommerce_integrations.medusa.order.order_sync.sync_order",
        "order.completed": "ecommerce_integrations.medusa.order.order_sync.sync_order",
        "customer.created": "ecommerce_integrations.medusa.customer.sync_customer_by_id",
        "customer.updated": "ecommerce_integrations.medusa.customer.sync_customer_by_id",
        "customer.deleted": "ecommerce_integrations.medusa.customer.handle_customer_deleted",
    }

    handler = handler_map.get(event_type)
    if handler:
        frappe.enqueue(
            handler,
            queue="default",
            entity_id=data.get("id"),
            payload=data,
            event_type=event_type,
            is_async=True,
        )

    # Trigger payment sync for events that may indicate payment changes
    # Only for updates/completions — order.placed won't have captured payments yet
    if event_type in ("order.updated", "order.completed"):
        frappe.enqueue(
            "ecommerce_integrations.medusa.payment_sync.sync_payment_for_order",
            queue="default",
            entity_id=data.get("id"),
            payload=data,
            event_type=event_type,
            is_async=True,
        )
