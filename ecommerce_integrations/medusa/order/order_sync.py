"""Sync Medusa orders to ERPNext Sales Orders."""
import frappe
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
            params={"fields": "+items,+items.variant,+items.variant.product,+shipping_address,+billing_address,+customer,+shipping_methods,+transactions"},
        )
        return result.get("order", {})

    def sync(self) -> str:
        if self.is_synced():
            return self.sales_order_name
        order_data = self.fetch_order()
        if not order_data:
            frappe.throw(f"Could not fetch Medusa order {self.order_id}")
        customer_id = order_data.get("customer_id")
        if customer_id and self.setting.sync_customers:
            mc = MedusaCustomer(customer_id)
            if not mc.is_synced():
                mc.sync_to_erpnext()
        so_data = map_medusa_order_to_so(order_data, self.setting)
        so = frappe.get_doc(so_data)
        so.flags.ignore_mandatory = True
        so.insert(ignore_permissions=True)
        so.submit()
        frappe.db.commit()
        self.sales_order_name = so.name
        return so.name


def sync_order(entity_id: str, event_type: str = ""):
    if not is_medusa_enabled():
        return
    log_name = create_medusa_log(
        request_type=f"Order Sync ({event_type})",
        medusa_id=entity_id,
        status="In Progress",
    )
    try:
        order = MedusaOrder(entity_id)
        if order.is_synced():
            update_medusa_log(log_name, status="Skipped", response_data={"message": "Already synced"})
            return
        so_name = order.sync()
        update_medusa_log(log_name, status="Success", response_data={"sales_order": so_name})
    except Exception as e:
        update_medusa_log(log_name, status="Error", error=str(e))
        frappe.log_error(f"Medusa order sync failed: {entity_id}", str(e))
