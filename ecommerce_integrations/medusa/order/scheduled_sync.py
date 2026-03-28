"""Scheduled order polling as fallback when webhooks fail."""
import frappe
from ecommerce_integrations.medusa.connection import medusa_request, temp_medusa_session
from ecommerce_integrations.medusa.constants import API_ORDERS, ORDER_ID_FIELD, SETTING_DOCTYPE
from ecommerce_integrations.medusa.order.order_sync import MedusaOrder
from ecommerce_integrations.medusa.utils import is_medusa_enabled


def sync_new_orders():
    if not is_medusa_enabled():
        return
    setting = frappe.get_cached_doc(SETTING_DOCTYPE)
    since = setting.last_order_sync or frappe.utils.add_days(frappe.utils.now_datetime(), -1)
    orders = _fetch_orders_since(since)
    for order_data in orders:
        order_id = order_data.get("id")
        if not order_id:
            continue
        if frappe.db.exists("Sales Order", {ORDER_ID_FIELD: order_id}):
            continue
        try:
            order = MedusaOrder(order_id)
            order.sync()
        except Exception as e:
            frappe.log_error(f"Medusa scheduled order sync failed: {order_id}", str(e))
    frappe.db.set_single_value(SETTING_DOCTYPE, "last_order_sync", frappe.utils.now_datetime())
    frappe.db.commit()


@temp_medusa_session
def _fetch_orders_since(session, base_url, since) -> list:
    result = medusa_request(
        session, base_url, "GET", API_ORDERS,
        params={"created_at[$gte]": str(since), "limit": 100, "order": "-created_at"},
    )
    return result.get("orders", [])
