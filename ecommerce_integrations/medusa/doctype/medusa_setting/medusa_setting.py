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

			from ecommerce_integrations.medusa.custom_fields import setup_custom_fields
			setup_custom_fields()

			# New scheduler_events in hooks.py stay dormant until sync_jobs runs.
			from frappe.core.doctype.scheduled_job_type.scheduled_job_type import sync_jobs
			sync_jobs()

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

	@frappe.whitelist()
	def fetch_stock_locations(self):
		"""Fetch stock locations from Medusa and create mappings.

		For each ERPNext warehouse in warehouse_mappings that has no
		Medusa Stock Location ID, creates a new location in Medusa.
		Also fetches existing locations and maps by name.
		"""
		from ecommerce_integrations.medusa.connection import get_medusa_session, medusa_request

		session, base_url = get_medusa_session()
		try:
			result = medusa_request(session, base_url, "GET", "/admin/stock-locations", params={"limit": 100})
			existing_locations = {loc["name"]: loc["id"] for loc in result.get("stock_locations", [])}

			created = 0
			for row in self.warehouse_mappings or []:
				wh_name = row.erpnext_warehouse
				if row.medusa_stock_location_id:
					continue

				if wh_name in existing_locations:
					row.medusa_stock_location_id = existing_locations[wh_name]
					row.medusa_stock_location_name = wh_name
				else:
					loc = medusa_request(session, base_url, "POST", "/admin/stock-locations", json={"name": wh_name})
					loc_data = loc.get("stock_location", {})
					if loc_data.get("id"):
						row.medusa_stock_location_id = loc_data["id"]
						row.medusa_stock_location_name = wh_name
						existing_locations[wh_name] = loc_data["id"]
						created += 1

			self.save()
			total = len([r for r in (self.warehouse_mappings or []) if r.medusa_stock_location_id])
			frappe.msgprint(
				_("{0} warehouse mappings configured ({1} new locations created in Medusa).").format(total, created),
				title=_("Stock Locations"),
				indicator="green",
			)
			return {"success": True, "total": total, "created": created}
		finally:
			session.close()

	def get_warehouse_location_map(self) -> dict:
		"""Return {erpnext_warehouse: medusa_stock_location_id} dict."""
		return {
			row.erpnext_warehouse: row.medusa_stock_location_id
			for row in (self.warehouse_mappings or [])
			if row.medusa_stock_location_id
		}

	def get_default_sales_channel_id(self):
		for row in self.sales_channels or []:
			if row.is_default:
				return row.sales_channel_id
		if self.sales_channels:
			return self.sales_channels[0].sales_channel_id
		return None

	def get_active_sales_channel_ids(self):
		return [row.sales_channel_id for row in (self.sales_channels or []) if row.active]

	def get_channels_for_item(self, item_group, brand=None) -> list:
		"""Get sales channel IDs for an item based on Item Group mappings.

		If no mappings are configured, returns all active channels.
		If mappings exist, returns only channels matching the item's group
		(with optional manufacturer/brand filter).
		"""
		mappings = self.item_group_channel_mappings or []
		if not mappings:
			return self.get_active_sales_channel_ids()

		# Build channel name -> ID lookup
		ch_lookup = {}
		for ch in (self.sales_channels or []):
			ch_lookup[ch.sales_channel_name] = ch.sales_channel_id
			if ch.short_code:
				ch_lookup[ch.short_code] = ch.sales_channel_id

		# Get item group ancestry for subcategory matching
		item_groups = {item_group}
		if item_group:
			lft_rgt = frappe.db.get_value("Item Group", item_group, ["lft", "rgt"])
			if lft_rgt:
				ancestors = frappe.get_all(
					"Item Group",
					filters={"lft": ["<=", lft_rgt[0]], "rgt": [">=", lft_rgt[1]]},
					fields=["name"],
				)
				item_groups.update(a.name for a in ancestors)

		matched_channels = set()
		for m in mappings:
			if m.include_subcategories:
				if m.item_group not in item_groups:
					continue
			else:
				if m.item_group != item_group:
					continue

			if m.manufacturer_filter and brand and m.manufacturer_filter != brand:
				continue

			ch_id = ch_lookup.get(m.sales_channel)
			if ch_id:
				matched_channels.add(ch_id)

		# Always include the default sales channel in addition to matched channels
		default = self.get_default_sales_channel_id()
		if default:
			matched_channels.add(default)

		if matched_channels:
			return list(matched_channels)

		# Last resort: return all active channels so products never end up without a channel
		return self.get_active_sales_channel_ids()
