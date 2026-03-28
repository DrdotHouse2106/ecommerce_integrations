"""Scheduled customer polling from Medusa as fallback when webhooks fail.

Runs every minute via Frappe scheduler, but only executes if
sync_customers is enabled and enough time has elapsed.
"""

import frappe
from frappe.utils import now_datetime, time_diff_in_seconds, add_days

from ecommerce_integrations.medusa.connection import medusa_request, temp_medusa_session
from ecommerce_integrations.medusa.constants import API_CUSTOMERS, CUSTOMER_ID_FIELD, SETTING_DOCTYPE
from ecommerce_integrations.medusa.customer import MedusaCustomer
from ecommerce_integrations.medusa.utils import is_medusa_enabled


def sync_new_customers():
	"""Scheduled job: pull new/updated customers from Medusa."""
	if not is_medusa_enabled():
		return

	setting = frappe.get_cached_doc(SETTING_DOCTYPE)
	if not setting.sync_customers:
		return

	frequency_minutes = int(setting.customer_sync_frequency or 30)

	if setting.last_customer_sync:
		elapsed = time_diff_in_seconds(now_datetime(), setting.last_customer_sync)
		if elapsed < frequency_minutes * 60:
			return

	since = setting.last_customer_sync or add_days(now_datetime(), -1)

	try:
		customers = _fetch_customers_since(since)
	except Exception as e:
		frappe.log_error(f"Medusa customer fetch failed: {e}", "Medusa Scheduled Customer Sync")
		return

	synced = 0
	for cust_data in customers:
		medusa_id = cust_data.get("id")
		if not medusa_id:
			continue
		try:
			customer = MedusaCustomer(medusa_id)
			customer.sync_to_erpnext(cust_data.get("customer", cust_data))
			synced += 1
		except Exception as e:
			frappe.log_error(f"Medusa scheduled customer sync failed: {medusa_id}", str(e))

	frappe.db.set_single_value(SETTING_DOCTYPE, "last_customer_sync", now_datetime())
	frappe.db.commit()

	if synced:
		frappe.logger("medusa").info(f"Scheduled sync: {synced} customers synced from Medusa")


@temp_medusa_session
def _fetch_customers_since(session, base_url, since) -> list:
	result = medusa_request(
		session, base_url, "GET", API_CUSTOMERS,
		params={"created_at[$gte]": str(since), "limit": 100, "order": "-created_at"},
	)
	return result.get("customers", [])
