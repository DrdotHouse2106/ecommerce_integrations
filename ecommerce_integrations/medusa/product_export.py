"""Export ERPNext Items to Medusa v2 as Products."""
import re

import frappe
from frappe.utils import cstr
from ecommerce_integrations.property_utils import get_ecommerce_properties
from ecommerce_integrations.medusa.connection import medusa_request, medusa_request_all, temp_medusa_session
from ecommerce_integrations.medusa.constants import (
    API_PRODUCTS, API_PRODUCTS_BATCH, API_PRODUCT_VARIANTS,
    PRODUCT_ID_FIELD, SETTING_DOCTYPE, VARIANT_ID_FIELD,
)
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
        if product.get("id"):
            _save_medusa_ids(product)
            frappe.db.commit()

    @temp_medusa_session
    def _update_product(self, session, base_url):
        medusa_id = self.get_medusa_product_id()
        payload = self._build_product_payload(is_update=True)
        medusa_request(session, base_url, "POST", f"{API_PRODUCTS}/{medusa_id}", json=payload)

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
        """Build Medusa product payload from ERPNext Item."""
        currency = (frappe.db.get_default("currency") or "EUR").lower()

        title = self.item.item_name
        description = self.item.description or self.item.item_name
        handle = self._make_handle(title)

        payload = {
            "title": title,
            "handle": handle,
            "description": description,
            "status": "published" if not self.item.disabled else "draft",
            "is_giftcard": False,
            "discountable": True,
        }

        for dim, field in [("weight", "weight_per_unit"), ("height", "item_height"),
                           ("width", "item_width"), ("length", "item_length")]:
            val = getattr(self.item, field, None)
            if val:
                payload[dim] = float(val)

        category_id = self._get_medusa_category_id()
        if category_id:
            payload["categories"] = [{"id": category_id}]

        if not is_update:
            channel_ids = self.setting.get_active_sales_channel_ids() if hasattr(self.setting, 'get_active_sales_channel_ids') else []
            if channel_ids:
                payload["sales_channels"] = [{"id": ch_id} for ch_id in channel_ids]

        meta = self._build_metadata()
        if meta:
            payload["metadata"] = meta

        if not is_update:
            if self.item.has_variants:
                options, variants, image_urls = self._build_template_variants(currency)
                payload["options"] = options
                payload["variants"] = variants
            else:
                image_urls = []
                img = self._get_image_url()
                if img:
                    image_urls.append(img)
                payload["options"] = [{"title": "Default", "values": ["Default"]}]
                payload["variants"] = [self._build_single_variant_payload(currency)]
        else:
            image_urls = []
            img = self._get_image_url()
            if img:
                image_urls.append(img)

        if image_urls:
            payload["images"] = [{"url": url} for url in image_urls]
            payload["thumbnail"] = image_urls[0]

        return {k: v for k, v in payload.items() if v is not None}

    def _build_metadata(self) -> dict:
        """Build metadata from AI fields, SEO fields and ecommerce properties."""
        meta = {}

        ai_fields = {
            "ai_seo_title": "seo_title",
            "ai_seo_description": "seo_description",
            "ai_short_description": "short_description",
            "ai_long_description": "long_description",
            "ai_benefits": "benefits",
            "ai_applications": "applications",
            "ai_delivery_scope": "delivery_scope",
        }
        for erpnext_field, meta_key in ai_fields.items():
            val = getattr(self.item, erpnext_field, None)
            if val:
                meta[meta_key] = val

        if not meta.get("seo_title"):
            val = getattr(self.item, "seo_title", None)
            if val:
                meta["seo_title"] = val
        if not meta.get("seo_description"):
            val = getattr(self.item, "seo_meta_description", None)
            if val:
                meta["seo_description"] = val
        seo_kw = getattr(self.item, "seo_keywords", None)
        if seo_kw:
            meta["seo_keywords"] = seo_kw

        prop_meta = self._get_medusa_metadata()
        if prop_meta:
            meta.update(prop_meta)

        return meta

    def _build_single_variant_payload(self, currency) -> dict:
        """Build variant payload for a simple (non-template) item."""
        return self._make_variant_dict(self.item, currency)

    def _build_template_variants(self, currency) -> tuple:
        """Build options, variants, and image URLs for a template item.

        Uses batch queries to avoid N+1: fetches all child items, their
        attributes, prices, and images in bulk.

        Returns (options, variant_payloads, image_urls).
        """
        attribute_names = [a.attribute for a in self.item.get("attributes", [])] or ["Default"]

        # Batch fetch all child data in 3 queries instead of N * get_doc
        child_codes_rows = frappe.get_all(
            "Item",
            filters={"variant_of": self.item_code, "disabled": 0},
            fields=["item_code", "item_name", "weight_per_unit", "item_height",
                     "item_width", "item_length", "customs_tariff_number",
                     "country_of_origin", "image"],
        )
        if not child_codes_rows:
            return [{"title": "Default", "values": ["Default"]}], [], []

        child_codes = [r.item_code for r in child_codes_rows]
        child_map = {r.item_code: r for r in child_codes_rows}

        # Batch fetch variant attributes
        variant_attrs = frappe.get_all(
            "Item Variant Attribute",
            filters={"parent": ["in", child_codes]},
            fields=["parent", "attribute", "attribute_value"],
        )
        attrs_by_item = {}
        for va in variant_attrs:
            attrs_by_item.setdefault(va.parent, {})[va.attribute] = va.attribute_value

        # Batch fetch prices
        price_list = self.setting.default_selling_price_list
        price_map = {}
        if price_list:
            prices = frappe.get_all(
                "Item Price",
                filters={"item_code": ["in", child_codes], "price_list": price_list, "selling": 1},
                fields=["item_code", "price_list_rate"],
            )
            price_map = {p.item_code: p.price_list_rate for p in prices}

        # Build variants and collect images + option values
        all_values = {attr: set() for attr in attribute_names}
        variant_payloads = []
        image_urls = []
        seen_images = set()

        # Template image first
        template_img = self._get_image_url()
        if template_img:
            image_urls.append(template_img)
            seen_images.add(template_img)

        for code in child_codes:
            child = child_map[code]
            option_values = attrs_by_item.get(code, {})

            for attr in attribute_names:
                if attr in option_values:
                    all_values[attr].add(option_values[attr])

            if not option_values and attribute_names == ["Default"]:
                option_values = {"Default": child.item_name}
                all_values["Default"].add(child.item_name)

            price = price_map.get(code, 0) or 0
            variant = {
                "title": child.item_name,
                "sku": code,
                "manage_inventory": True,
                "allow_backorder": False,
                "prices": [{"currency_code": (frappe.db.get_default("currency") or "EUR").lower(),
                            "amount": erpnext_price_to_medusa(price)}],
                "options": option_values,
            }

            for dim, field in [("weight", "weight_per_unit"), ("height", "item_height"),
                               ("width", "item_width"), ("length", "item_length")]:
                val = child.get(field)
                if val:
                    variant[dim] = float(val)
            if child.customs_tariff_number:
                variant["hs_code"] = child.customs_tariff_number
            if child.country_of_origin:
                variant["origin_country"] = self._get_country_code(child.country_of_origin)

            variant_payloads.append(variant)

            # Collect variant image
            if child.image:
                url = self._resolve_image_url(child.image)
                if url and url not in seen_images:
                    image_urls.append(url)
                    seen_images.add(url)

        options = []
        for attr_name in attribute_names:
            values = sorted(all_values.get(attr_name, set()))
            if values:
                options.append({"title": attr_name, "values": values})

        if not options:
            options = [{"title": "Default", "values": ["Default"]}]

        return options, variant_payloads, image_urls

    def _make_variant_dict(self, item_data, currency) -> dict:
        """Build a variant dict from item data (doc or dict-like)."""
        price = self._get_selling_price(item_data.item_code if hasattr(item_data, 'item_code') else item_data.get('item_code'))
        variant = {
            "title": item_data.item_name if hasattr(item_data, 'item_name') else item_data.get('item_name', ''),
            "sku": item_data.item_code if hasattr(item_data, 'item_code') else item_data.get('item_code', ''),
            "manage_inventory": True,
            "allow_backorder": False,
            "prices": [{"currency_code": currency, "amount": erpnext_price_to_medusa(price)}],
        }
        for dim, field in [("weight", "weight_per_unit"), ("height", "item_height"),
                           ("width", "item_width"), ("length", "item_length")]:
            val = getattr(item_data, field, None) or (item_data.get(field) if hasattr(item_data, 'get') else None)
            if val:
                variant[dim] = float(val)
        hs = getattr(item_data, 'customs_tariff_number', None)
        if hs:
            variant["hs_code"] = hs
        co = getattr(item_data, 'country_of_origin', None)
        if co:
            variant["origin_country"] = self._get_country_code(co)
        return variant

    def _make_handle(self, title) -> str:
        handle_base = re.sub(r'[^a-z0-9-]', '-', title.lower())
        handle_base = re.sub(r'-+', '-', handle_base).strip('-')
        sku_slug = re.sub(r'[^a-z0-9-]', '-', self.item.item_code.lower()).strip('-')
        return f"{handle_base}-{sku_slug}"

    def _get_medusa_category_id(self):
        from ecommerce_integrations.medusa.constants import API_CATEGORIES

        item_group = self.item.item_group
        if not item_group:
            return None

        all_cats_key = "medusa_category_map"
        cat_map = frappe.cache.get_value(all_cats_key)
        if not cat_map:
            from ecommerce_integrations.medusa.connection import get_medusa_session
            session, base_url = get_medusa_session()
            try:
                categories = medusa_request_all(session, base_url, API_CATEGORIES, "product_categories")
                cat_map = {c["name"]: c["id"] for c in categories}
                frappe.cache.set_value(all_cats_key, cat_map, expires_in_sec=300)
            except Exception:
                cat_map = {}
            finally:
                session.close()

        return cat_map.get(item_group)

    def _get_image_url(self):
        return self._resolve_image_url(self.item.image)

    @staticmethod
    def _resolve_image_url(image_path):
        """Resolve ERPNext image path to public URL (S3/CDN or site URL)."""
        if not image_path:
            return None
        if "/private/" in image_path:
            return None
        if image_path.startswith("http"):
            return image_path

        file_data = frappe.db.get_value(
            "File", {"file_url": image_path},
            ["dfp_external_storage", "dfp_external_storage_s3_key"], as_dict=True
        )
        if file_data and file_data.dfp_external_storage_s3_key and file_data.dfp_external_storage:
            storage = _get_dfp_storage_config(file_data.dfp_external_storage)
            if storage:
                return f"https://{storage.endpoint}/{storage.bucket_name}/{file_data.dfp_external_storage_s3_key}"

        return f"{frappe.utils.get_url().rstrip('/')}{image_path}"

    @staticmethod
    def _get_country_code(country_name: str) -> str:
        if not country_name:
            return ""
        code = frappe.db.get_value("Country", country_name, "code")
        return (code or "").upper()

    def _get_medusa_metadata(self) -> dict:
        metadata = {}
        for row in get_ecommerce_properties(self.item, "sync_to_medusa"):
            if row.property_value:
                key = f"custom_{row.property_name}" if row.property_type == 'Custom Field' else row.property_name
                metadata[key] = cstr(row.property_value).strip()
        return metadata

    def _get_selling_price(self, item_code=None) -> float:
        price_list = self.setting.default_selling_price_list
        if not price_list:
            return 0.0
        code = item_code or self.item_code
        price = frappe.db.get_value("Item Price", {"item_code": code, "price_list": price_list, "selling": 1}, "price_list_rate")
        return price or 0.0


def _get_dfp_storage_config(storage_name):
    """Get DFP External Storage config (cached)."""
    cache_key = f"dfp_storage:{storage_name}"
    cached = frappe.cache.get_value(cache_key)
    if cached:
        return cached
    storage = frappe.db.get_value(
        "DFP External Storage", storage_name,
        ["bucket_name", "endpoint"], as_dict=True
    )
    if storage and storage.endpoint:
        frappe.cache.set_value(cache_key, storage, expires_in_sec=3600)
    return storage


def _save_medusa_ids(product: dict):
    """Save Medusa product/variant IDs back to ERPNext Items.

    For template products: saves product ID to template, variant IDs to child items.
    For simple products: saves both to the same item.
    """
    product_id = product.get("id")
    if not product_id:
        return

    variants = product.get("variants", [])
    for variant in variants:
        sku = variant.get("sku")
        variant_id = variant.get("id")
        if not sku or not frappe.db.exists("Item", sku):
            continue

        # Check if this item is a variant (has variant_of)
        variant_of = frappe.db.get_value("Item", sku, "variant_of")
        if variant_of:
            # Child variant: save product ID to template, variant ID to child
            frappe.db.set_value("Item", variant_of, PRODUCT_ID_FIELD, product_id)
            if variant_id:
                frappe.db.set_value("Item", sku, VARIANT_ID_FIELD, variant_id)
        else:
            # Simple item: save both to same item
            frappe.db.set_value("Item", sku, PRODUCT_ID_FIELD, product_id)
            if variant_id:
                frappe.db.set_value("Item", sku, VARIANT_ID_FIELD, variant_id)


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
	"""Run a complete sync of all products from ERPNext to Medusa using batch API."""
	from ecommerce_integrations.medusa.connection import get_medusa_session

	stats = {"created": 0, "updated": 0, "errors": 0, "skipped": 0}
	batch_size = int(batch_size)

	if sync_categories:
		try:
			cat_result = _sync_categories_to_medusa(dry_run=dry_run)
			frappe.logger("medusa").info(f"Category sync: {cat_result}")
		except Exception as e:
			frappe.log_error("Medusa Full Sync", f"Category sync failed: {e}")

	if sync_products or sync_prices:
		setting = frappe.get_cached_doc(SETTING_DOCTYPE)
		filters = {"disabled": 0}
		if setting.category_sync_root:
			filters["item_group"] = ["descendants of (inclusive)", setting.category_sync_root]

		# Sync both simple items and templates
		items = frappe.get_all("Item", filters=filters, fields=["item_code", PRODUCT_ID_FIELD, "has_variants", "variant_of"], limit=0)

		# Exclude child variants (they are synced via their template)
		items = [i for i in items if not i.variant_of]

		if dry_run:
			stats["skipped"] = len(items)
		else:
			session, base_url = get_medusa_session()
			try:
				for chunk_start in range(0, len(items), batch_size):
					chunk = items[chunk_start:chunk_start + batch_size]
					create_batch = []
					update_batch = []

					for item_row in chunk:
						try:
							exporter = MedusaProductExporter(item_row.item_code)
							payload = exporter._build_product_payload(is_update=bool(item_row.get(PRODUCT_ID_FIELD)))

							if item_row.get(PRODUCT_ID_FIELD):
								payload["id"] = item_row.get(PRODUCT_ID_FIELD)
								update_batch.append(payload)
							else:
								create_batch.append(payload)
						except Exception as e:
							stats["errors"] += 1
							frappe.log_error("Medusa Full Sync", f"Payload build failed for {item_row.item_code}: {e}")

					if create_batch or update_batch:
						try:
							batch_payload = {}
							if create_batch:
								batch_payload["create"] = create_batch
							if update_batch:
								batch_payload["update"] = update_batch

							result = medusa_request(session, base_url, "POST", API_PRODUCTS_BATCH, json=batch_payload)

							for product in result.get("created", []):
								_save_medusa_ids(product)
								stats["created"] += 1

							stats["updated"] += len(result.get("updated", []))
						except Exception as e:
							stats["errors"] += len(create_batch) + len(update_batch)
							frappe.log_error("Medusa Full Sync", f"Batch sync failed: {e}")

					frappe.db.commit()
			finally:
				session.close()

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

	existing_cats = medusa_request_all(session, base_url, API_CATEGORIES, "product_categories")
	existing_by_name = {c.get("name"): c.get("id") for c in existing_cats}

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
