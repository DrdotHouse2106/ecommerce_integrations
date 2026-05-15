"""Sync Medusa orders to ERPNext Sales Orders."""
import frappe
from frappe import _

from ecommerce_integrations.medusa.connection import medusa_request, temp_medusa_session
from ecommerce_integrations.medusa.constants import API_ORDERS, ORDER_ID_FIELD, SETTING_DOCTYPE
from ecommerce_integrations.medusa.customer import MedusaCustomer
from ecommerce_integrations.medusa.order.order_mapper import map_medusa_order_to_so
from ecommerce_integrations.medusa.utils import create_medusa_log, is_medusa_enabled, update_medusa_log


class MedusaOrder:
    def __init__(self, order_id: str):
        self.order_id = order_id
        self.setting = frappe.get_cached_doc(SETTING_DOCTYPE)
        self.sales_order_name = self._get_existing_sales_order()

    def _get_existing_sales_order(self):
        return frappe.db.get_value("Sales Order", {ORDER_ID_FIELD: self.order_id})

    def is_synced(self) -> bool:
        return bool(self.sales_order_name)

    @temp_medusa_session
    def fetch_order(self, session, base_url) -> dict:
        result = medusa_request(
            session, base_url, "GET",
            f"{API_ORDERS}/{self.order_id}",
            params={"fields": "+items,+items.variant,+items.variant.product,+shipping_address,+billing_address,+shipping_methods,+transactions,+payment_collections,+payment_collections.payments,+payment_collections.payment_sessions,+payment_collections.payment_providers,*customer,*customer.*,*sales_channel"},
        )
        return result.get("order", {})

    def sync(self, order_data: dict | None = None) -> str:
        if self.is_synced():
            return self.sales_order_name
        if order_data is None:
            order_data = self.fetch_order()
        if not order_data:
            frappe.throw(_("Could not fetch Medusa order {0}").format(self.order_id))

        # Sync customer first so the lookup in the mapper succeeds.
        # Prefer the embedded customer object when available (skips an API round-trip).
        customer_obj = order_data.get("customer") or {}
        customer_id = customer_obj.get("id") or order_data.get("customer_id")
        if customer_id:
            mc = MedusaCustomer(customer_id)
            if not mc.is_synced():
                mc.sync_to_erpnext(customer_obj or None)

        so_data = map_medusa_order_to_so(order_data, self.setting)
        so = frappe.get_doc(so_data)
        so.flags.ignore_mandatory = True
        so.insert(ignore_permissions=True)
        frappe.db.commit()

        # Mark as confirmed to trigger "Bestellbestätigung" notification
        so.ecommerce_order_confirmed = 1
        so.save(ignore_permissions=True)
        frappe.db.commit()

        self.sales_order_name = so.name
        return so.name


def _extract_order(payload) -> dict | None:
    """Pull the order dict out of a webhook payload, if enriched."""
    if isinstance(payload, dict):
        order = payload.get("order")
        if isinstance(order, dict) and order.get("id"):
            return order
    return None


def sync_order(entity_id: str | None = None, payload: dict | None = None, event_type: str = ""):
    """Entry point for webhook- and scheduler-triggered order sync.

    Accepts either an enriched ``payload`` (with an embedded ``order`` object)
    or a bare ``entity_id`` (in which case the order is fetched via the Admin
    API).
    """
    if not is_medusa_enabled():
        return

    order_data = _extract_order(payload)
    medusa_id = (order_data or {}).get("id") or entity_id
    if not medusa_id:
        return

    log_name = create_medusa_log(
        request_type=f"Order Sync ({event_type})",
        medusa_id=medusa_id,
        status="In Progress",
    )
    try:
        order = MedusaOrder(medusa_id)
        if order.is_synced():
            update_medusa_log(log_name, status="Skipped", response_data={"message": "Already synced"})
            return
        so_name = order.sync(order_data)
        update_medusa_log(log_name, status="Success", response_data={"sales_order": so_name})
    except Exception as e:
        update_medusa_log(log_name, status="Error", error=str(e))
        frappe.log_error(f"Medusa order sync failed: {medusa_id}", str(e))
