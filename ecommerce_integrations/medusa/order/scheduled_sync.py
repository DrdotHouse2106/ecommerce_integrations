"""Scheduled order polling as fallback when webhooks fail.

Runs every minute via Frappe scheduler, but only executes the actual
sync if enough time has elapsed (controlled by order_sync_frequency).
"""

import frappe
from frappe.utils import add_days, now_datetime, time_diff_in_seconds

from ecommerce_integrations.medusa.connection import medusa_request, temp_medusa_session
from ecommerce_integrations.medusa.constants import API_ORDERS, ORDER_ID_FIELD, SETTING_DOCTYPE
from ecommerce_integrations.medusa.order.order_sync import MedusaOrder
from ecommerce_integrations.medusa.utils import is_medusa_enabled


def sync_new_orders():
	"""Scheduled job: sync orders from Medusa.

	Called every minute by scheduler, but respects order_sync_frequency
	to avoid excessive API calls.
	"""
	if not is_medusa_enabled():
		return

	setting = frappe.get_cached_doc(SETTING_DOCTYPE)
	frequency_minutes = int(setting.order_sync_frequency or 30)

	if setting.last_order_sync:
		elapsed = time_diff_in_seconds(now_datetime(), setting.last_order_sync)
		if elapsed < frequency_minutes * 60:
			return

	since = setting.last_order_sync or add_days(now_datetime(), -1)

	try:
		orders = _fetch_orders_since(since)
	except Exception as e:
		frappe.log_error(f"Medusa order fetch failed: {e}", "Medusa Scheduled Order Sync")
		return

	synced = 0
	for order_data in orders:
		order_id = order_data.get("id")
		if not order_id:
			continue
		if frappe.db.exists("Sales Order", {ORDER_ID_FIELD: order_id}):
			continue
		try:
			order = MedusaOrder(order_id)
			order.sync()
			synced += 1
		except Exception as e:
			frappe.log_error(f"Medusa scheduled order sync failed: {order_id}", str(e))

	frappe.db.set_single_value(SETTING_DOCTYPE, "last_order_sync", now_datetime())
	frappe.db.commit()

	if synced:
		frappe.logger("medusa").info(f"Scheduled sync: {synced} orders synced from Medusa")


@temp_medusa_session
def _fetch_orders_since(session, base_url, since) -> list:
	result = medusa_request(
		session, base_url, "GET", API_ORDERS,
		params={"created_at[$gte]": str(since), "limit": 100, "order": "-created_at"},
	)
	return result.get("orders", [])
