"""Export ERPNext Items to Medusa v2 as Products."""
import frappe
from frappe.utils import cstr
from ecommerce_integrations.property_utils import get_ecommerce_properties
from ecommerce_integrations.medusa.connection import medusa_request, temp_medusa_session
from ecommerce_integrations.medusa.constants import API_PRODUCTS, API_PRODUCT_VARIANTS, PRODUCT_ID_FIELD, SETTING_DOCTYPE, VARIANT_ID_FIELD
from ecommerce_integrations.medusa.utils import create_medusa_log, erpnext_price_to_medusa, is_medusa_enabled, update_medusa_log


class MedusaProductExporter:
    def __init__(self, item_code: str):
        self.item_code = item_code
        self.item = frappe.get_doc("Item", item_code)
        self.setting = frappe.get_cached_doc(SETTING_DOCTYPE)

    def get_medusa_product_id(self):
        return self.item.get(PRODUCT_ID_FIELD)

    def is_synced(self) -> bool:
        return bool(self.get_medusa_product_id())

    def export(self):
        if self.is_synced():
            self._update_product()
        else:
            self._create_product()

    @temp_medusa_session
    def _create_product(self, session, base_url):
        payload = self._build_product_payload()
        result = medusa_request(session, base_url, "POST", API_PRODUCTS, json=payload)
        product = result.get("product", {})
        medusa_id = product.get("id")
        if medusa_id:
            frappe.db.set_value("Item", self.item_code, PRODUCT_ID_FIELD, medusa_id)
            variants = product.get("variants", [])
            if variants:
                frappe.db.set_value("Item", self.item_code, VARIANT_ID_FIELD, variants[0].get("id"))
            frappe.db.commit()

    @temp_medusa_session
    def _update_product(self, session, base_url):
        medusa_id = self.get_medusa_product_id()
        payload = self._build_product_payload(is_update=True)
        medusa_request(session, base_url, "PUT", f"{API_PRODUCTS}/{medusa_id}", json=payload)

    @temp_medusa_session
    def update_price(self, session, base_url):
        """Update variant price for an already-synced product."""
        medusa_product_id = self.get_medusa_product_id()
        variant_id = self.item.get(VARIANT_ID_FIELD)
        if not medusa_product_id or not variant_id:
            return

        price = self._get_selling_price()
        currency = (frappe.db.get_default("currency") or "EUR").lower()
        endpoint = API_PRODUCT_VARIANTS.format(product_id=medusa_product_id) + f"/{variant_id}"
        medusa_request(session, base_url, "POST", endpoint, json={
            "prices": [{"currency_code": currency, "amount": erpnext_price_to_medusa(price)}],
        })

    def _build_product_payload(self, is_update=False) -> dict:
        price = self._get_selling_price()
        payload = {
            "title": self.item.item_name,
            "description": self.item.description or self.item.item_name,
            "status": "published" if not self.item.disabled else "draft",
            "is_giftcard": False,
            "discountable": True,
        }
        # Ecommerce properties -> Medusa metadata
        metadata = self._get_medusa_metadata()
        if metadata:
            payload["metadata"] = metadata

        if not is_update:
            options = self._get_medusa_options()
            payload["options"] = options if options else [{"title": "Default", "values": ["Default"]}]
            payload["variants"] = [{
                "title": self.item.item_name,
                "sku": self.item.item_code,
                "manage_inventory": True,
                "allow_backorder": False,
                "prices": [{
                    "currency_code": (frappe.db.get_default("currency") or "EUR").lower(),
                    "amount": erpnext_price_to_medusa(price),
                }],
            }]
        if self.item.weight_per_unit:
            payload["weight"] = int(self.item.weight_per_unit * 1000)
        return payload

    def _get_medusa_metadata(self) -> dict:
        """Get ecommerce_properties marked for Medusa sync as product metadata."""
        metadata = {}
        for row in get_ecommerce_properties(self.item, "sync_to_medusa"):
            if row.property_value:
                if row.property_type == 'Custom Field':
                    metadata[f"custom_{row.property_name}"] = cstr(row.property_value).strip()
                else:
                    metadata[row.property_name] = cstr(row.property_value).strip()
        return metadata

    def _get_medusa_options(self) -> list:
        """Get Item Attributes marked for Medusa sync as Product Options."""
        if not self.item.has_variants:
            return []

        options = []
        for attr_row in self.item.get("attributes", []):
            attr_doc = frappe.get_cached_doc("Item Attribute", attr_row.attribute)
            if not getattr(attr_doc, 'sync_to_medusa', 0):
                continue
            if getattr(attr_doc, 'medusa_property_type', '') != 'Option':
                continue

            values = [v.attribute_value for v in attr_doc.item_attribute_values]
            if values:
                options.append({"title": attr_row.attribute, "values": values})

        return options

    def _get_selling_price(self) -> float:
        price_list = self.setting.default_selling_price_list
        if not price_list:
            return 0.0
        price = frappe.db.get_value("Item Price", {"item_code": self.item_code, "price_list": price_list, "selling": 1}, "price_list_rate")
        return price or 0.0


@frappe.whitelist()
def enqueue_full_sync(sync_categories=1, sync_products=1, sync_prices=1, sync_stock=0, batch_size=50, dry_run=0):
	"""Enqueue a full product sync to Medusa as a background job."""
	if not is_medusa_enabled():
		return {"success": False, "message": "Medusa integration is not enabled"}

	frappe.enqueue(
		"ecommerce_integrations.medusa.product_export.run_full_sync",
		queue="long",
		timeout=3600,
		sync_categories=int(sync_categories),
		sync_products=int(sync_products),
		sync_prices=int(sync_prices),
		sync_stock=int(sync_stock),
		batch_size=int(batch_size),
		dry_run=int(dry_run),
	)
	return {"success": True, "message": "Full sync enqueued"}


def run_full_sync(sync_categories=1, sync_products=1, sync_prices=1, sync_stock=0, batch_size=50, dry_run=0):
	"""Run a complete sync of all products from ERPNext to Medusa."""
	stats = {"synced": 0, "errors": 0, "skipped": 0}
	batch_size = int(batch_size)

	if sync_categories:
		try:
			cat_result = _sync_categories_to_medusa(dry_run=dry_run)
			frappe.logger("medusa").info(f"Category sync: {cat_result}")
		except Exception as e:
			frappe.log_error("Medusa Full Sync", f"Category sync failed: {e}")

	if sync_products or sync_prices:
		setting = frappe.get_cached_doc(SETTING_DOCTYPE)
		filters = {"disabled": 0, "has_variants": 0}
		if setting.category_sync_root:
			filters["item_group"] = ["descendants of (inclusive)", setting.category_sync_root]

		items = frappe.get_all("Item", filters=filters, fields=["item_code"], limit=0)

		for i, item in enumerate(items):
			if dry_run:
				stats["skipped"] += 1
				continue
			try:
				exporter = MedusaProductExporter(item.item_code)
				if sync_products:
					exporter.export()
				if sync_prices and exporter.is_synced():
					exporter.update_price()
				stats["synced"] += 1
			except Exception as e:
				stats["errors"] += 1
				frappe.log_error("Medusa Full Sync", f"Product sync failed for {item.item_code}: {e}")

			if (i + 1) % batch_size == 0:
				frappe.db.commit()

		frappe.db.commit()

	if sync_stock and not dry_run:
		try:
			from ecommerce_integrations.medusa.inventory import sync_inventory_to_medusa
			sync_inventory_to_medusa()
		except Exception as e:
			frappe.log_error("Medusa Full Sync", f"Inventory sync failed: {e}")

	frappe.logger("medusa").info(f"Full sync complete: {stats}")
	return stats


@frappe.whitelist()
def sync_categories(category_root=None, dry_run=0):
	"""Sync ERPNext Item Groups as Medusa product categories."""
	if not is_medusa_enabled():
		return {"total": 0, "synced": 0, "errors": 0}
	return _sync_categories_to_medusa(category_root=category_root, dry_run=int(dry_run))


@temp_medusa_session
def _sync_categories_to_medusa(session, base_url, category_root=None, dry_run=0):
	"""Push Item Groups to Medusa as product categories."""
	from ecommerce_integrations.medusa.constants import API_CATEGORIES

	setting = frappe.get_cached_doc(SETTING_DOCTYPE)
	root = category_root or setting.category_sync_root or "All Item Groups"

	groups = frappe.get_all(
		"Item Group",
		filters={"parent_item_group": ["descendants of (inclusive)", root]},
		fields=["name", "parent_item_group", "is_group"],
		order_by="lft",
	)

	stats = {"total": len(groups), "synced": 0, "errors": 0}
	if dry_run:
		return stats

	existing = medusa_request(session, base_url, "GET", API_CATEGORIES, params={"limit": 0})
	existing_by_name = {c.get("name"): c.get("id") for c in existing.get("product_categories", [])}

	for group in groups:
		if group.name in existing_by_name:
			stats["synced"] += 1
			continue
		try:
			parent_id = existing_by_name.get(group.parent_item_group)
			payload = {"name": group.name, "is_active": True}
			if parent_id:
				payload["parent_category_id"] = parent_id

			result = medusa_request(session, base_url, "POST", API_CATEGORIES, json=payload)
			cat = result.get("product_category", {})
			if cat.get("id"):
				existing_by_name[group.name] = cat["id"]
				stats["synced"] += 1
		except Exception as e:
			stats["errors"] += 1
			frappe.log_error("Medusa Category Sync", f"Failed for {group.name}: {e}")

	return stats


@frappe.whitelist()
def enqueue_force_price_sync():
	"""Enqueue re-push of all prices to Medusa."""
	if not is_medusa_enabled():
		return {"success": False, "message": "Medusa integration is not enabled"}

	frappe.enqueue(
		"ecommerce_integrations.medusa.product_export._run_force_price_sync",
		queue="long",
		timeout=1800,
	)
	return {"success": True, "message": "Price sync enqueued"}


def _run_force_price_sync():
	"""Re-push all prices for synced products via variant price update."""
	items = frappe.get_all(
		"Item",
		filters={PRODUCT_ID_FIELD: ["is", "set"], VARIANT_ID_FIELD: ["is", "set"], "disabled": 0},
		fields=["item_code"],
	)
	synced = 0
	for i, item in enumerate(items):
		try:
			exporter = MedusaProductExporter(item.item_code)
			exporter.update_price()
			synced += 1
		except Exception as e:
			frappe.log_error("Medusa Price Sync", f"Price sync failed for {item.item_code}: {e}")

		if (i + 1) % 50 == 0:
			frappe.db.commit()

	frappe.db.commit()
	frappe.logger("medusa").info(f"Force price sync complete: {synced}/{len(items)} updated")


def upload_item_to_medusa(doc, method=None):
    if not is_medusa_enabled():
        return
    setting = frappe.get_cached_doc(SETTING_DOCTYPE)
    if method == "after_insert" and not setting.upload_erpnext_items:
        return
    if method == "on_update" and not setting.sync_item_on_update:
        return
    try:
        exporter = MedusaProductExporter(doc.name)
        exporter.export()
    except Exception as e:
        frappe.log_error(f"Medusa product export failed: {doc.name}", str(e))
