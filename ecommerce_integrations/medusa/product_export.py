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
        """Build Medusa product payload from ERPNext Item.

        For template items (has_variants=1): creates product with options
        and all child variants inline. For simple items: creates product
        with a single default variant.
        """
        currency = (frappe.db.get_default("currency") or "EUR").lower()

        # Direct ERPNext fields
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

        # Dimensions
        for dim, field in [("weight", "weight_per_unit"), ("height", "item_height"),
                           ("width", "item_width"), ("length", "item_length")]:
            val = getattr(self.item, field, None)
            if val:
                payload[dim] = float(val)

        # Category
        category_id = self._get_medusa_category_id()
        if category_id:
            payload["categories"] = [{"id": category_id}]

        # Sales channels (on create only)
        if not is_update:
            channel_ids = self.setting.get_active_sales_channel_ids() if hasattr(self.setting, 'get_active_sales_channel_ids') else []
            if channel_ids:
                payload["sales_channels"] = [{"id": ch_id} for ch_id in channel_ids]

        # All AI + SEO fields go into metadata for storefront consumption
        meta = self._build_metadata()
        if meta:
            payload["metadata"] = meta

        # Images: collect all unique images (template + variants)
        if self.item.has_variants:
            image_urls = self._collect_template_images()
        else:
            image_urls = []
            img = self._get_image_url()
            if img:
                image_urls.append(img)

        if image_urls:
            payload["images"] = [{"url": url} for url in image_urls]
            payload["thumbnail"] = image_urls[0]

        # Variants
        if not is_update:
            if self.item.has_variants:
                options, variants = self._build_template_variants(currency)
                payload["options"] = options
                payload["variants"] = variants
            else:
                payload["options"] = [{"title": "Default", "values": ["Default"]}]
                payload["variants"] = [self._build_variant_payload(self.item, currency)]

        return {k: v for k, v in payload.items() if v is not None}

    def _build_metadata(self) -> dict:
        """Build metadata dict from AI fields, SEO fields and ecommerce properties."""
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

        # Standard SEO fields as fallback (only if AI version not set)
        if not meta.get("seo_title"):
            seo_title = getattr(self.item, "seo_title", None)
            if seo_title:
                meta["seo_title"] = seo_title
        if not meta.get("seo_description"):
            seo_desc = getattr(self.item, "seo_meta_description", None)
            if seo_desc:
                meta["seo_description"] = seo_desc
        seo_keywords = getattr(self.item, "seo_keywords", None)
        if seo_keywords:
            meta["seo_keywords"] = seo_keywords

        # Ecommerce properties
        prop_meta = self._get_medusa_metadata()
        if prop_meta:
            meta.update(prop_meta)

        return meta

    def _build_variant_payload(self, item_doc, currency, option_values=None) -> dict:
        """Build a single variant payload from an ERPNext Item."""
        price = self._get_selling_price(item_doc.item_code)
        variant = {
            "title": item_doc.item_name,
            "sku": item_doc.item_code,
            "manage_inventory": True,
            "allow_backorder": False,
            "prices": [{"currency_code": currency, "amount": erpnext_price_to_medusa(price)}],
        }

        if option_values:
            variant["options"] = option_values

        for dim, field in [("weight", "weight_per_unit"), ("height", "item_height"),
                           ("width", "item_width"), ("length", "item_length")]:
            val = getattr(item_doc, field, None)
            if val:
                variant[dim] = float(val)

        if item_doc.customs_tariff_number:
            variant["hs_code"] = item_doc.customs_tariff_number
        if item_doc.country_of_origin:
            variant["origin_country"] = self._get_country_code(item_doc.country_of_origin)

        return variant

    def _build_template_variants(self, currency) -> tuple:
        """Build options and variants arrays for a template item.

        Returns (options, variants) where options defines the attribute
        axes and variants are the child items with their option values.
        """
        child_items = frappe.get_all(
            "Item",
            filters={"variant_of": self.item_code, "disabled": 0},
            fields=["item_code"],
        )

        # Collect unique attributes from child items
        attribute_names = []
        for attr in self.item.get("attributes", []):
            attribute_names.append(attr.attribute)

        if not attribute_names:
            attribute_names = ["Default"]

        # Build option values per attribute from actual variants
        all_values = {attr: set() for attr in attribute_names}
        variant_payloads = []

        for child in child_items:
            child_doc = frappe.get_doc("Item", child.item_code)

            # Get this variant's attribute values
            option_values = {}
            for va in child_doc.get("attributes", []):
                if va.attribute in all_values:
                    all_values[va.attribute].add(va.attribute_value)
                    option_values[va.attribute] = va.attribute_value

            if not option_values and attribute_names == ["Default"]:
                option_values = {"Default": child_doc.item_name}
                all_values["Default"].add(child_doc.item_name)

            variant_payloads.append(
                self._build_variant_payload(child_doc, currency, option_values)
            )

        options = []
        for attr_name in attribute_names:
            values = sorted(all_values.get(attr_name, set()))
            if values:
                options.append({"title": attr_name, "values": values})

        if not options:
            options = [{"title": "Default", "values": ["Default"]}]

        return options, variant_payloads

    def _collect_template_images(self) -> list:
        """Collect all unique image URLs from template and its variants."""
        urls = []
        seen = set()

        # Template image first
        template_img = self._get_image_url()
        if template_img:
            urls.append(template_img)
            seen.add(template_img)

        # Variant images
        child_items = frappe.get_all(
            "Item",
            filters={"variant_of": self.item_code, "disabled": 0, "image": ["is", "set"]},
            fields=["image"],
        )
        for child in child_items:
            url = self._resolve_image_url(child.image)
            if url and url not in seen:
                urls.append(url)
                seen.add(url)

        return urls

    def _make_handle(self, title) -> str:
        """Generate a URL-safe handle from title + item_code."""
        handle_base = re.sub(r'[^a-z0-9-]', '-', title.lower())
        handle_base = re.sub(r'-+', '-', handle_base).strip('-')
        sku_slug = re.sub(r'[^a-z0-9-]', '-', self.item.item_code.lower()).strip('-')
        return f"{handle_base}-{sku_slug}"

    def _get_medusa_category_id(self) -> str:
        """Look up the Medusa category ID for this item's Item Group."""
        from ecommerce_integrations.medusa.constants import API_CATEGORIES

        item_group = self.item.item_group
        if not item_group:
            return None

        cache_key = f"medusa_category_id:{item_group}"
        cached = frappe.cache.get_value(cache_key)
        if cached:
            return cached

        # Batch-fetch all categories once and cache them
        all_cats_key = "medusa_category_map"
        cat_map = frappe.cache.get_value(all_cats_key)
        if not cat_map:
            from ecommerce_integrations.medusa.connection import get_medusa_session, medusa_request
            session, base_url = get_medusa_session()
            try:
                categories = medusa_request_all(session, base_url, API_CATEGORIES, "product_categories")
                cat_map = {c["name"]: c["id"] for c in categories}
                frappe.cache.set_value(all_cats_key, cat_map, expires_in_sec=300)
            except Exception:
                cat_map = {}
            finally:
                session.close()

        cat_id = cat_map.get(item_group)
        if cat_id:
            frappe.cache.set_value(cache_key, cat_id, expires_in_sec=300)
        return cat_id

    def _get_image_url(self):
        """Get public URL for this item's image."""
        return self._resolve_image_url(self.item.image)

    @staticmethod
    def _resolve_image_url(image_path):
        """Resolve an ERPNext image path to a public URL.

        For S3-backed files (dfp_external_storage): builds CDN URL.
        For relative paths: prepends site URL.
        """
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
            storage = frappe.db.get_value(
                "DFP External Storage", file_data.dfp_external_storage,
                ["bucket_name", "endpoint"], as_dict=True
            )
            if storage and storage.endpoint:
                return f"https://{storage.endpoint}/{storage.bucket_name}/{file_data.dfp_external_storage_s3_key}"

        return f"{frappe.utils.get_url().rstrip('/')}{image_path}"

    @staticmethod
    def _get_country_code(country_name: str) -> str:
        """Convert ERPNext country name to ISO 2-letter code."""
        if not country_name:
            return ""
        code = frappe.db.get_value("Country", country_name, "code")
        return (code or "").upper()

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

    def _get_selling_price(self, item_code=None) -> float:
        price_list = self.setting.default_selling_price_list
        if not price_list:
            return 0.0
        code = item_code or self.item_code
        price = frappe.db.get_value("Item Price", {"item_code": code, "price_list": price_list, "selling": 1}, "price_list_rate")
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
		filters = {"disabled": 0, "has_variants": 0}
		if setting.category_sync_root:
			filters["item_group"] = ["descendants of (inclusive)", setting.category_sync_root]

		items = frappe.get_all("Item", filters=filters, fields=["item_code", PRODUCT_ID_FIELD], limit=0)

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


def _save_medusa_ids(product: dict):
	"""Save Medusa product/variant IDs back to ERPNext Item."""
	variants = product.get("variants", [])
	if not variants:
		return
	sku = variants[0].get("sku")
	if not sku:
		return
	if not frappe.db.exists("Item", sku):
		return
	frappe.db.set_value("Item", sku, PRODUCT_ID_FIELD, product.get("id"))
	if variants[0].get("id"):
		frappe.db.set_value("Item", sku, VARIANT_ID_FIELD, variants[0]["id"])


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
