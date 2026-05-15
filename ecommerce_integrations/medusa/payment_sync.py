"""Sync payment status from Medusa to ERPNext.

Creates Payment Entries when Medusa order payment_status changes to 'captured'.
Optionally creates Sales Invoices for paid orders.
"""

import frappe

from ecommerce_integrations.controllers.scheduling import need_to_run
from ecommerce_integrations.medusa.connection import medusa_request, temp_medusa_session
from ecommerce_integrations.medusa.constants import API_ORDERS, ORDER_ID_FIELD, SETTING_DOCTYPE
from ecommerce_integrations.medusa.utils import create_medusa_log, is_medusa_enabled, update_medusa_log


def sync_payment_for_order(entity_id: str = None, payload: dict = None, event_type: str = ""):
	"""Handle payment status change for a single order (webhook-triggered).

	The ``payload`` kwarg is accepted for parity with the webhook handler but
	currently unused — payment_collections must be fetched via the Admin API
	since the subscriber payload does not include them.
	"""
	if not is_medusa_enabled():
		return
	entity_id = entity_id or (payload or {}).get("id")
	if not entity_id:
		return

	setting = frappe.get_cached_doc(SETTING_DOCTYPE)
	if not setting.sync_payment_status:
		return

	log_name = create_medusa_log(
		request_type=f"Payment Sync ({event_type})",
		medusa_id=entity_id,
		status="In Progress",
	)

	try:
		result = _process_order_payment(entity_id, setting)
		if result:
			update_medusa_log(log_name, status="Success", response_data=result)
		else:
			update_medusa_log(log_name, status="Skipped", response_data={"message": "No payment action needed"})
	except Exception as e:
		update_medusa_log(log_name, status="Error", error=str(e))
		frappe.log_error(f"Medusa payment sync failed: {entity_id}", str(e))


@temp_medusa_session
def _fetch_order_payment_status(session, base_url, order_id: str) -> dict:
	"""Fetch order with payment status and transaction data from Medusa."""
	result = medusa_request(
		session, base_url, "GET",
		f"{API_ORDERS}/{order_id}",
		params={"fields": "+payment_collections,+payment_collections.payments"},
	)
	return result.get("order", {})


def _has_payment_entry(so_name: str) -> bool:
	"""Check if a non-cancelled Payment Entry exists for a Sales Order."""
	return bool(frappe.db.sql("""
		SELECT 1 FROM `tabPayment Entry Reference` per
		JOIN `tabPayment Entry` pe ON pe.name = per.parent
		WHERE per.reference_doctype = 'Sales Order'
		AND per.reference_name = %s
		AND pe.docstatus != 2
		LIMIT 1
	""", so_name))


def _process_order_payment(medusa_order_id: str, setting) -> dict:
	"""Check payment status and create Payment Entry if captured."""
	so_name = frappe.db.get_value("Sales Order", {ORDER_ID_FIELD: medusa_order_id})
	if not so_name:
		return None

	if _has_payment_entry(so_name):
		return None

	order_data = _fetch_order_payment_status(medusa_order_id)
	if not order_data:
		return None

	payment_status = order_data.get("payment_status", "")
	if payment_status not in ("captured", "partially_captured"):
		return None

	result = {}

	pe_name = _create_payment_entry(so_name, order_data, setting)
	result["payment_entry"] = pe_name

	if setting.sync_sales_invoice and payment_status == "captured":
		si_name = _create_sales_invoice(so_name, setting)
		if si_name:
			result["sales_invoice"] = si_name

	frappe.db.commit()
	return result


def _create_payment_entry(so_name: str, order_data: dict, setting) -> str:
	"""Create a Payment Entry for a captured Medusa order."""
	from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

	from ecommerce_integrations.medusa.payment_method_mapping import (
		extract_provider_id,
		resolve_mode_of_payment,
	)

	pe = get_payment_entry("Sales Order", so_name, bank_account=setting.cash_bank_account)

	so_mode = frappe.db.get_value("Sales Order", so_name, "mode_of_payment")
	mode_of_payment = so_mode or resolve_mode_of_payment(extract_provider_id(order_data), setting)
	if mode_of_payment:
		pe.mode_of_payment = mode_of_payment

	# Extract payment reference from Stripe/payment provider data
	payment_reference = ""
	for pc in order_data.get("payment_collections", []):
		for payment in pc.get("payments", []):
			payment_reference = (payment.get("data") or {}).get("id", "")
			if payment_reference:
				break
		if payment_reference:
			break

	pe.reference_no = payment_reference or order_data.get("id", "")
	pe.reference_date = frappe.utils.today()
	pe.remarks = f"Payment for Medusa order {order_data.get('id', '')}"
	pe.flags.ignore_mandatory = True
	pe.insert(ignore_permissions=True)
	pe.submit()

	return pe.name


def _create_sales_invoice(so_name: str, setting) -> str:
	"""Create a Sales Invoice from a paid Sales Order."""
	existing_si = frappe.db.get_value("Sales Invoice Item", {"sales_order": so_name, "docstatus": ["!=", 2]}, "parent")
	if existing_si:
		return None

	from erpnext.selling.doctype.sales_order.sales_order import make_sales_invoice

	si = make_sales_invoice(so_name)
	si.flags.ignore_mandatory = True
	si.insert(ignore_permissions=True)
	si.submit()

	if setting.auto_send_invoice_email:
		_send_invoice_email(si.name)

	return si.name


def _send_invoice_email(invoice_name: str):
	"""Send invoice PDF via email to the customer."""
	try:
		si = frappe.get_doc("Sales Invoice", invoice_name)
		if not si.contact_email:
			return
		frappe.sendmail(
			recipients=[si.contact_email],
			subject=f"Invoice {si.name}",
			message=f"Please find your invoice {si.name} attached.",
			attachments=[frappe.attach_print("Sales Invoice", si.name, print_format=None)],
			reference_doctype="Sales Invoice",
			reference_name=si.name,
		)
	except Exception as e:
		frappe.log_error(f"Failed to send invoice email for {invoice_name}", str(e))


# --- Scheduled sync (fallback) ---

def sync_payment_status():
	"""Scheduled job: check payment status of open orders in Medusa.

	Called every minute by scheduler, but respects payment_sync_frequency.
	Only runs if sync_payment_status is enabled in Medusa Setting.
	"""
	if not is_medusa_enabled():
		return

	setting = frappe.get_cached_doc(SETTING_DOCTYPE)
	if not setting.sync_payment_status:
		return

	if not need_to_run(SETTING_DOCTYPE, "payment_sync_frequency", "last_payment_sync"):
		return

	# SAFE: ORDER_ID_FIELD is a hardcoded module-level constant ("medusa_order_id"),
	# not user input. f-string interpolation here is identifier substitution
	# (column name), not value substitution, so %s placeholders are not applicable.
	open_orders = frappe.db.sql(f"""
		SELECT so.name, so.{ORDER_ID_FIELD} as medusa_order_id
		FROM `tabSales Order` so
		WHERE so.{ORDER_ID_FIELD} IS NOT NULL
		AND so.{ORDER_ID_FIELD} != ''
		AND so.docstatus = 1
		AND so.status NOT IN ('Cancelled', 'Closed')
		AND NOT EXISTS (
			SELECT 1 FROM `tabPayment Entry Reference` per
			JOIN `tabPayment Entry` pe ON pe.name = per.parent
			WHERE per.reference_doctype = 'Sales Order'
			AND per.reference_name = so.name
			AND pe.docstatus != 2
		)
		ORDER BY so.creation DESC
		LIMIT 50
	""", as_dict=True)

	synced = 0
	for order in open_orders:
		try:
			result = _process_order_payment(order.medusa_order_id, setting)
			if result:
				synced += 1
		except Exception as e:
			frappe.log_error(f"Medusa payment sync failed: {order.medusa_order_id}", str(e))

	if synced:
		frappe.logger("medusa").info(f"Scheduled payment sync: {synced} payments created")
