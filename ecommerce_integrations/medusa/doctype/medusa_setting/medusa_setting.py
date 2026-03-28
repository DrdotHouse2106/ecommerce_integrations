import frappe
from frappe import _
from frappe.model.document import Document


class MedusaSetting(Document):

	def onload(self):
		self.load_series_options()

	def validate(self):
		if self.enable_medusa:
			if not self.medusa_url:
				frappe.throw(_("Medusa URL is required when integration is enabled"))
			if not self.api_key:
				frappe.throw(_("API Key is required when integration is enabled"))

	def load_series_options(self):
		series_fields = {
			"sales_order_series": "Sales Order",
			"delivery_note_series": "Delivery Note",
		}
		for key, doctype in series_fields.items():
			options = frappe.get_meta(doctype).get_options("naming_series") or ""
			self.set_onload(f"{key}_options", options)

	def is_enabled(self) -> bool:
		return bool(self.enable_medusa)

	@frappe.whitelist()
	def test_connection(self):
		from ecommerce_integrations.medusa.connection import test_connection

		result = test_connection()
		if result.get("success"):
			frappe.msgprint(
				_("Connection successful. {0} products found in Medusa.").format(result.get("product_count", 0)),
				title=_("Success"),
				indicator="green",
			)
		else:
			frappe.msgprint(
				_("Connection failed: {0}").format(result.get("message", "")),
				title=_("Error"),
				indicator="red",
			)
		return result

	@frappe.whitelist()
	def fetch_sales_channels(self):
		from ecommerce_integrations.medusa.connection import get_medusa_session, medusa_request

		session, base_url = get_medusa_session()
		try:
			result = medusa_request(session, base_url, "GET", "/admin/sales-channels", params={"limit": 100})
		finally:
			session.close()

		channels = result.get("sales_channels", [])

		existing = {row.sales_channel_id: row for row in self.sales_channels or []}

		self.sales_channels = []
		for ch in channels:
			ch_id = ch.get("id")
			if ch_id in existing:
				self.append("sales_channels", {
					"sales_channel_id": ch_id,
					"sales_channel_name": ch.get("name", ""),
					"active": existing[ch_id].active,
					"is_default": existing[ch_id].is_default,
				})
			else:
				self.append("sales_channels", {
					"sales_channel_id": ch_id,
					"sales_channel_name": ch.get("name", ""),
					"active": 1,
					"is_default": 0,
				})

		self.save()
		frappe.msgprint(
			_("{0} sales channels fetched from Medusa.").format(len(channels)),
			title=_("Sales Channels"),
			indicator="green",
		)
		return {"success": True, "count": len(channels)}

	def get_default_sales_channel_id(self):
		for row in self.sales_channels or []:
			if row.is_default:
				return row.sales_channel_id
		if self.sales_channels:
			return self.sales_channels[0].sales_channel_id
		return None

	def get_active_sales_channel_ids(self):
		return [row.sales_channel_id for row in (self.sales_channels or []) if row.active]
