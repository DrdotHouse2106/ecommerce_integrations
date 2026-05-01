"""Propagate the ecommerce channel from a Sales Order into downstream docs.

When ERPNext creates a Sales Invoice / Delivery Note / Payment Entry from a
Sales Order, the ``ecommerce_source`` / ``ecommerce_sales_channel_id`` /
``ecommerce_sales_channel_name`` fields are not part of the standard mapping
and would silently be empty on the downstream doc. The ChannelAwareNotification
override then can't resolve the channel and falls back to default behaviour
(no branding, no language switch, no E-Invoice attachment).

These hooks copy the channel info from the originating Sales Order so the
notification + branding logic just works.
"""

from typing import Iterable

import frappe


CHANNEL_FIELDS = ("ecommerce_source", "ecommerce_sales_channel_id", "ecommerce_sales_channel_name")


def _copy_channel_fields(target, channel_values: dict):
	"""Set ecommerce_* fields on target if not already set."""
	if not channel_values:
		return
	changed = False
	for field in CHANNEL_FIELDS:
		val = channel_values.get(field)
		if val and not target.get(field):
			target.set(field, val)
			changed = True
	return changed


def _channel_from_sales_orders(so_names: Iterable[str]) -> dict:
	"""Pull the ecommerce_* fields from one of the linked Sales Orders.

	If multiple SOs were merged into the doc, we use the first one with
	non-empty channel info — they should all share a channel for ecommerce
	flows anyway.
	"""
	for so_name in so_names:
		if not so_name:
			continue
		row = frappe.db.get_value("Sales Order", so_name, list(CHANNEL_FIELDS), as_dict=True)
		if row and row.get("ecommerce_sales_channel_name"):
			return row
	return {}


def propagate_to_sales_invoice(doc, method=None):
	"""Hook on Sales Invoice (validate / before_insert)."""
	if doc.get("ecommerce_sales_channel_name"):
		return
	so_names = {item.get("sales_order") for item in (doc.items or []) if item.get("sales_order")}
	if not so_names:
		return
	_copy_channel_fields(doc, _channel_from_sales_orders(so_names))


def propagate_to_delivery_note(doc, method=None):
	"""Hook on Delivery Note (validate / before_insert)."""
	if doc.get("ecommerce_sales_channel_name"):
		return
	so_names = {item.get("against_sales_order") for item in (doc.items or []) if item.get("against_sales_order")}
	if not so_names:
		return
	_copy_channel_fields(doc, _channel_from_sales_orders(so_names))


def propagate_to_payment_entry(doc, method=None):
	"""Hook on Payment Entry (validate / before_insert).

	Looks at payment references — Sales Order direct, or Sales Invoice → Sales Order.
	"""
	if doc.get("ecommerce_sales_channel_name"):
		return

	so_names = set()
	si_names = set()
	for ref in (doc.get("references") or []):
		dt = ref.get("reference_doctype")
		name = ref.get("reference_name")
		if not name:
			continue
		if dt == "Sales Order":
			so_names.add(name)
		elif dt == "Sales Invoice":
			si_names.add(name)

	channel = _channel_from_sales_orders(so_names) if so_names else {}

	if not channel and si_names:
		# Fall back: Sales Invoice already carries the channel (after this hook
		# fires for invoices), so just copy from there.
		for si in si_names:
			row = frappe.db.get_value("Sales Invoice", si, list(CHANNEL_FIELDS), as_dict=True)
			if row and row.get("ecommerce_sales_channel_name"):
				channel = row
				break

	if not channel and si_names:
		# Last resort: walk SI items to find a Sales Order link.
		for si in si_names:
			rows = frappe.db.sql(
				"SELECT DISTINCT sales_order FROM `tabSales Invoice Item` WHERE parent=%s AND sales_order IS NOT NULL AND sales_order != ''",
				(si,),
			)
			so_from_si = {r[0] for r in rows}
			channel = _channel_from_sales_orders(so_from_si)
			if channel:
				break

	_copy_channel_fields(doc, channel)
