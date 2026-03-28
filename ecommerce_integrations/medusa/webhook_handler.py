"""Handle incoming events from the Medusa subscriber plugin.

Endpoint: /api/method/ecommerce_integrations.medusa.webhook_handler.handle_medusa_event

Expected payload:
{
    "event": "order.placed",
    "data": {"id": "order_01ABC..."},
    "timestamp": "2026-03-28T12:00:00Z"
}
"""

import hashlib
import hmac
import json

import frappe

from ecommerce_integrations.medusa.constants import SETTING_DOCTYPE
from ecommerce_integrations.medusa.utils import create_medusa_log, is_medusa_enabled, update_medusa_log


@frappe.whitelist(allow_guest=True)
def handle_medusa_event():
    """Receive and process webhook events from Medusa subscriber plugin."""
    if not is_medusa_enabled():
        frappe.throw("Medusa integration is not enabled", frappe.AuthenticationError)

    payload = frappe.request.get_data(as_text=True)
    if not payload:
        frappe.throw("Empty payload", frappe.ValidationError)

    setting = frappe.get_cached_doc(SETTING_DOCTYPE)
    webhook_secret = setting.get_password("webhook_secret") if setting.webhook_secret else None

    if webhook_secret:
        signature = frappe.request.headers.get("x-medusa-webhook-signature", "")
        expected = hmac.new(
            webhook_secret.encode("utf-8"),
            payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            frappe.throw("Invalid webhook signature", frappe.AuthenticationError)

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
    }

    handler = handler_map.get(event_type)
    if handler:
        frappe.enqueue(
            handler,
            queue="default",
            entity_id=data.get("id"),
            event_type=event_type,
            is_async=True,
        )
