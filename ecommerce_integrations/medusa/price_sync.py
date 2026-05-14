"""Medusa Price List helpers extracted from ``product_export.py``.

Wrappers around the ``/admin/price-lists/*`` endpoints. All functions
take an ``optional_session``-style ``(session, base_url)`` pair so the
exporter can hand them an already-authenticated requests session and
they don't open a new one per call.

Public surface intentionally matches what ``product_export.py`` exposed
before the split — ``sync_channel_prices`` / ``sync_sale_prices`` and
their ``_batch`` variants. ``product_export.py`` imports them with the
leading-underscore alias so existing callers don't need to change.
"""

import frappe

from ecommerce_integrations.medusa.connection import medusa_request, medusa_request_all


def sku_to_variant_id(product: dict) -> dict:
	"""Build ``{sku: variant_id}`` map from a Medusa product."""
	return {v["sku"]: v["id"] for v in product.get("variants", []) if v.get("sku") and v.get("id")}


def upsert_price_list_entries(
	session,
	base_url,
	price_list_id: str,
	entries: list,
	log_label: str = "Medusa Prices",
):
	"""Replace price entries for given variants in a Medusa price list.

	Deletes existing entries for the affected variants, then creates new ones.
	"""
	if not entries or not price_list_id:
		return

	try:
		variant_ids = {e["variant_id"] for e in entries}
		existing_pl = medusa_request(
			session, base_url, "GET",
			f"/admin/price-lists/{price_list_id}",
			params={"fields": "+prices"},
		)
		existing_prices = existing_pl.get("price_list", {}).get("prices", [])
		delete_ids = [p["id"] for p in existing_prices if p.get("variant_id") in variant_ids]

		batch = {"create": entries}
		if delete_ids:
			batch["delete"] = delete_ids

		medusa_request(
			session, base_url, "POST",
			f"/admin/price-lists/{price_list_id}/prices/batch",
			json=batch,
		)
	except Exception as e:
		frappe.log_error(log_label, f"Failed to upsert prices in {price_list_id}: {e}")


def sync_channel_prices(session, base_url, product: dict, channel_prices: list):
	"""Sync per-channel prices as Medusa Price Lists (type override)."""
	if not channel_prices:
		return
	sync_channel_prices_batch(session, base_url, [(product, channel_prices)])


def sync_channel_prices_batch(session, base_url, product_channel_prices: list):
	"""Batch sync channel prices for multiple products.

	Groups all entries by channel, then syncs each channel's price list once.
	"""
	if not product_channel_prices:
		return

	by_channel = {}
	for product, channel_prices in product_channel_prices:
		vid_map = sku_to_variant_id(product)
		for cp in channel_prices:
			ch_id = cp["channel_id"]
			by_channel.setdefault(ch_id, {"name": cp["channel_name"], "entries": []})
			variant_id = vid_map.get(cp["sku"])
			if variant_id:
				by_channel[ch_id]["entries"].append({
					"variant_id": variant_id,
					"currency_code": cp["currency_code"],
					"amount": cp["amount"],
				})

	for ch_id, data in by_channel.items():
		if not data["entries"]:
			continue
		pl_id = get_or_create_channel_price_list(session, base_url, ch_id, data["name"])
		if pl_id:
			upsert_price_list_entries(session, base_url, pl_id, data["entries"], "Medusa Channel Prices")


def get_or_create_channel_price_list(session, base_url, channel_id: str, channel_name: str) -> str:
	"""Get or create a Price List for a specific sales channel."""
	cache_key = f"medusa_channel_pl:{channel_id}"
	cached = frappe.cache.get_value(cache_key)
	if cached:
		return cached

	title = f"Channel: {channel_name}"

	result = medusa_request_all(session, base_url, "/admin/price-lists", "price_lists")
	for pl in result:
		if pl.get("title") == title:
			frappe.cache.set_value(cache_key, pl["id"], expires_in_sec=3600)
			return pl["id"]

	try:
		result = medusa_request(session, base_url, "POST", "/admin/price-lists", json={
			"title": title,
			"description": f"Channel-specific prices for {channel_name}",
			"type": "override",
			"status": "active",
		})
		pl_id = result.get("price_list", {}).get("id")
		if pl_id:
			frappe.cache.set_value(cache_key, pl_id, expires_in_sec=3600)
		return pl_id
	except Exception as e:
		frappe.log_error("Medusa Channel Prices", f"Failed to create price list for {channel_name}: {e}")
		return None


def sync_sale_prices(session, base_url, product: dict, sale_prices: list):
	"""Add sale prices to a Medusa Price List for strikethrough display."""
	if not sale_prices:
		return
	sync_sale_prices_batch(session, base_url, [(product, sale_prices)])


def sync_sale_prices_batch(session, base_url, product_sale_prices: list):
	"""Batch sync sale prices for multiple products in one API call."""
	all_entries = []
	for product, sale_prices in product_sale_prices:
		vid_map = sku_to_variant_id(product)
		for sp in sale_prices:
			variant_id = vid_map.get(sp["sku"])
			if variant_id:
				all_entries.append({
					"variant_id": variant_id,
					"currency_code": sp["currency_code"],
					"amount": sp["amount"],
				})

	if not all_entries:
		return

	price_list_id = get_or_create_sale_price_list(session, base_url)
	if price_list_id:
		upsert_price_list_entries(session, base_url, price_list_id, all_entries, "Medusa Sale Prices")


def get_or_create_sale_price_list(session, base_url) -> str:
	"""Get or create the ERPNext sale price list in Medusa."""
	cache_key = "medusa_sale_price_list_id"
	cached = frappe.cache.get_value(cache_key)
	if cached:
		return cached

	# Search for existing
	result = medusa_request_all(session, base_url, "/admin/price-lists", "price_lists")
	for pl in result:
		if pl.get("title") == "ERPNext Sale Prices" and pl.get("type") == "sale":
			frappe.cache.set_value(cache_key, pl["id"], expires_in_sec=3600)
			return pl["id"]

	# Create new
	try:
		result = medusa_request(session, base_url, "POST", "/admin/price-lists", json={
			"title": "ERPNext Sale Prices",
			"description": "Sale prices synced from ERPNext selling price list",
			"type": "sale",
			"status": "active",
		})
		pl_id = result.get("price_list", {}).get("id")
		if pl_id:
			frappe.cache.set_value(cache_key, pl_id, expires_in_sec=3600)
		return pl_id
	except Exception as e:
		frappe.log_error("Medusa Sale Prices", f"Failed to create sale price list: {e}")
		return None
