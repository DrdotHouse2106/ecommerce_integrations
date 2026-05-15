"""Push ERPNext document status changes to Medusa v2."""
import frappe

from ecommerce_integrations.medusa.connection import medusa_request, temp_medusa_session
from ecommerce_integrations.medusa.constants import API_FULFILLMENTS, API_ORDERS, ORDER_ID_FIELD
from ecommerce_integrations.medusa.utils import is_medusa_enabled


def on_sales_order_cancel(doc, method=None):
    if not is_medusa_enabled():
        return
    medusa_order_id = doc.get(ORDER_ID_FIELD)
    if not medusa_order_id:
        return
    try:
        _cancel_medusa_order(medusa_order_id)
    except Exception as e:
        frappe.log_error(f"Failed to cancel Medusa order {medusa_order_id}", str(e))


def on_delivery_note_submit(doc, method=None):
    if not is_medusa_enabled():
        return
    for item in doc.items:
        if item.against_sales_order:
            medusa_order_id = frappe.db.get_value("Sales Order", item.against_sales_order, ORDER_ID_FIELD)
            if medusa_order_id:
                try:
                    _create_medusa_fulfillment(medusa_order_id, doc)
                except Exception as e:
                    frappe.log_error(f"Failed to create Medusa fulfillment for {medusa_order_id}", str(e))
                break


@temp_medusa_session
def _cancel_medusa_order(session, base_url, medusa_order_id: str):
    medusa_request(session, base_url, "POST", f"{API_ORDERS}/{medusa_order_id}/cancel")


@temp_medusa_session
def _create_medusa_fulfillment(session, base_url, medusa_order_id: str, delivery_note):
    result = medusa_request(session, base_url, "GET", f"{API_ORDERS}/{medusa_order_id}", params={"fields": "+items"})
    order = result.get("order", {})
    order_items = order.get("items", [])
    if not order_items:
        return
    items = [{"id": item["id"], "quantity": item["quantity"]} for item in order_items]
    medusa_request(session, base_url, "POST", API_FULFILLMENTS.format(order_id=medusa_order_id), json={"items": items})
