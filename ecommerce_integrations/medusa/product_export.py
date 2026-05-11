"""Export ERPNext Items to Medusa v2 as Products."""
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

import frappe
import requests
from frappe.utils import cstr

from ecommerce_integrations.ecommerce_integrations.media import is_image_url
from ecommerce_integrations.medusa._attributes import (
    ensure_attributes_exist as _ensure_attributes_exist,
    get_category_map as _get_category_map,
    get_or_build_attribute_map as _get_or_build_attribute_map,
    resolve_attribute_values as _resolve_attribute_values,
    transliterate as _transliterate,
)
from ecommerce_integrations.medusa.connection import medusa_request, medusa_request_all, optional_session, temp_medusa_session
from ecommerce_integrations.medusa.constants import (
    API_FEATURE_FLAGS, API_INDEX_SYNC, API_INDEX_DETAILS, API_PRODUCTS, API_PRODUCTS_BATCH,
    API_PRODUCT_VARIANTS, PRODUCT_ID_FIELD, SETTING_DOCTYPE, VARIANT_ID_FIELD,
)
from ecommerce_integrations.medusa.utils import create_medusa_log, erpnext_price_to_medusa, is_medusa_enabled, update_medusa_log
from ecommerce_integrations.property_utils import get_ecommerce_properties

class MedusaProductExporter:
    def __init__(self, item_code: str):
        self.item_code = item_code
        self.item = frappe.get_doc("Item", item_code)
        self.setting = frappe.get_cached_doc(SETTING_DOCTYPE)
        self._sale_prices = []
        self._channel_prices = []
        self._variants_to_move = []
        self._variants_to_delete = []
        self._attribute_values = []

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
        payload, variant_image_map = self._build_product_payload()
        # Single create supports additional_data via workflow hook
        if self._attribute_values:
            payload["additional_data"] = {"values": self._attribute_values}
        result = medusa_request(session, base_url, "POST", API_PRODUCTS, json=payload)
        product = result.get("product", {})
        if product.get("id"):
            _save_medusa_ids(product)
            if variant_image_map:
                _associate_variant_images(session, base_url, product, variant_image_map)
            if self._sale_prices:
                _sync_sale_prices(session, base_url, product, self._sale_prices)
            if self._channel_prices:
                _sync_channel_prices(session, base_url, product, self._channel_prices)
            frappe.db.commit()

    @temp_medusa_session
    def _update_product(self, session, base_url):
        medusa_id = self.get_medusa_product_id()
        payload, _ = self._build_product_payload(is_update=True)
        medusa_request(session, base_url, "POST", f"{API_PRODUCTS}/{medusa_id}", json=payload)
        if self._variants_to_delete:
            _delete_variants(session, base_url, medusa_id, self._variants_to_delete)

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

    def _build_product_payload(self, is_update=False, session=None, base_url=None) -> tuple:
        """Build Medusa product payload from ERPNext Item.

        Returns (payload_dict, variant_image_map) where variant_image_map
        is {sku: image_url} for post-create variant-image association.
        """
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

        category_ids = self._get_medusa_category_ids(session=session, base_url=base_url)
        if category_ids:
            payload["categories"] = [{"id": cid} for cid in category_ids]

        if hasattr(self.setting, 'get_channels_for_item'):
            # Smart-Collections-driven (replaces the legacy Item Group
            # Channel Mappings — see the Smart Collections section on the
            # Medusa Setting form).
            channel_ids = self.setting.get_channels_for_item(self.item_code)
        else:
            channel_ids = self.setting.get_active_sales_channel_ids() if hasattr(self.setting, 'get_active_sales_channel_ids') else []
        if not channel_ids:
            # Log to Ecommerce Integration Log and raise a plain exception. The
            # bulk-sync queue catches this per-item, so a single misconfigured
            # item doesn't break the rest of the batch. Avoid frappe.throw here:
            # the build path runs from queue workers as well as web requests, and
            # a UI popup would surface to whichever user happened to be saving an
            # Item at the wrong moment (CLAUDE.md: errors during sync are logged,
            # never raised back to the user).
            msg = (
                f"No Medusa Sales Channel could be determined for Item {self.item_code} "
                f"(Item Group: {self.item.item_group}). "
                "Please check Medusa Settings: ensure at least one Sales Channel is "
                "active or configure Item Group Channel Mappings."
            )
            create_medusa_log(status="Error", message=msg)
            raise ValueError(msg)
        payload["sales_channels"] = [{"id": ch_id} for ch_id in channel_ids]

        # Collect attribute values (for post-create batch-assign)
        self._attribute_values = self._get_attribute_values(session=session, base_url=base_url)

        meta = self._build_metadata()
        if meta:
            payload["metadata"] = meta

        variant_image_map = {}

        if self.item.has_variants:
            options, variants, image_urls, variant_image_map = self._build_template_variants(currency, is_update=is_update)
            payload["options"] = options
            payload["variants"] = variants
        else:
            image_urls = self._get_all_image_urls()
            if not is_update:
                payload["options"] = [{"title": "Default", "values": ["Default"]}]
            variant = self._build_single_variant_payload(currency)
            if is_update:
                sku = variant.get("sku")
                medusa_pid = self.item.get(PRODUCT_ID_FIELD)
                sku_map = frappe.cache.get_value("medusa_sku_map") or {}
                entry = sku_map.get(sku)
                if entry and entry.get("product_id") == medusa_pid:
                    variant["id"] = entry["variant_id"]
                elif entry:
                    self._variants_to_move.append(entry)
            payload["variants"] = [variant]

        if image_urls:
            payload["images"] = [{"url": url} for url in image_urls]
            payload["thumbnail"] = image_urls[0]

        return {k: v for k, v in payload.items() if v is not None}, variant_image_map

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
        # For templates without AI data, fall back to first variant that has it
        ai_field_names = list(ai_fields.keys())
        source = self.item
        if self.item.has_variants and not any(getattr(self.item, f, None) for f in ai_field_names):
            first_variant = frappe.db.get_value(
                "Item", {"variant_of": self.item_code, "disabled": 0, "ai_short_description": ["is", "set"]},
                ai_field_names + ["seo_title", "seo_meta_description", "seo_keywords"],
                as_dict=True,
            )
            if first_variant:
                source = first_variant

        for erpnext_field, meta_key in ai_fields.items():
            val = getattr(source, erpnext_field, None)
            if val:
                meta[meta_key] = val

        if not meta.get("seo_title"):
            val = getattr(source, "seo_title", None)
            if val:
                meta["seo_title"] = val
        if not meta.get("seo_description"):
            val = getattr(source, "seo_meta_description", None)
            if val:
                meta["seo_description"] = val
        seo_kw = getattr(source, "seo_keywords", None)
        if seo_kw:
            meta["seo_keywords"] = seo_kw

        prop_meta = self._get_medusa_metadata()
        if prop_meta:
            meta.update(prop_meta)

        # Delivery time
        delivery_time = self.item.get("delivery_time")
        if delivery_time:
            meta["delivery_time"] = delivery_time.strip()

        # Return sorted: AI fields first, then SEO, then properties (alphabetical)
        ai_keys = {"short_description", "long_description", "benefits", "applications", "delivery_scope"}
        seo_keys = {"seo_title", "seo_description", "seo_keywords"}

        sorted_meta = {}
        for k in ["short_description", "long_description", "benefits", "applications", "delivery_scope"]:
            if k in meta:
                sorted_meta[k] = meta[k]
        for k in ["seo_title", "seo_description", "seo_keywords"]:
            if k in meta:
                sorted_meta[k] = meta[k]
        for k in sorted(meta.keys()):
            if k not in ai_keys and k not in seo_keys:
                sorted_meta[k] = meta[k]

        return sorted_meta

    def _build_single_variant_payload(self, currency) -> dict:
        """Build variant payload for a simple (non-template) item."""
        return self._make_variant_dict(self.item, currency)

    def _build_template_variants(self, currency, is_update=False) -> tuple:
        """Build options, variants, and image URLs for a template item.

        Uses batch queries to avoid N+1: fetches all child items, their
        attributes, prices, and images in bulk.

        Returns (options, variant_payloads, image_urls, variant_image_map).
        """
        attribute_names = [a.attribute for a in self.item.get("attributes", [])] or ["Default"]

        # {sku: image_url} for variant-image association
        variant_image_map = {}

        # Batch fetch all child data in 3 queries instead of N * get_doc
        child_codes_rows = frappe.get_all(
            "Item",
            filters={"variant_of": self.item_code},
            fields=["item_code", "item_name", "weight_per_unit", "item_height",
                     "item_width", "item_length", "customs_tariff_number",
                     "country_of_origin", "delivered_by_supplier", "image",
                     "ai_short_description", "ai_long_description",
                     "ai_benefits", "ai_applications", "ai_delivery_scope",
                     "ai_seo_title", "ai_seo_description", "seo_keywords",
                     "delivery_time", "disabled",
                     VARIANT_ID_FIELD],
        )
        if not child_codes_rows:
            return [{"title": "Default", "values": ["Default"]}], [], []

        # Split disabled variants out: they will be deleted from Medusa post-update
        disabled_rows = [r for r in child_codes_rows if r.disabled]
        for r in disabled_rows:
            vid = r.get(VARIANT_ID_FIELD)
            if vid:
                self._variants_to_delete.append({"sku": r.item_code, "variant_id": vid})
        child_codes_rows = [r for r in child_codes_rows if not r.disabled]
        if not child_codes_rows:
            return [{"title": "Default", "values": ["Default"]}], [], []

        child_codes = [r.item_code for r in child_codes_rows]
        child_map = {r.item_code: r for r in child_codes_rows}

        # Batch fetch ecommerce properties for all variants
        variant_ecom_props = frappe.get_all(
            "Item Ecommerce Property",
            filters={"parent": ["in", child_codes], "parenttype": "Item", "sync_to_medusa": 1},
            fields=["parent", "property_name", "property_value", "property_type", "filterable"],
        )
        ecom_props_by_item = {}
        for prop in variant_ecom_props:
            ecom_props_by_item.setdefault(prop.parent, []).append(prop)

        # Batch fetch variant attributes
        variant_attrs = frappe.get_all(
            "Item Variant Attribute",
            filters={"parent": ["in", child_codes]},
            fields=["parent", "attribute", "attribute_value"],
        )
        attrs_by_item = {}
        for va in variant_attrs:
            attrs_by_item.setdefault(va.parent, {})[va.attribute] = va.attribute_value

        # Batch fetch selling prices and list/RRP prices
        price_map = {}
        list_price_map = {}
        if self.setting.default_selling_price_list:
            prices = frappe.get_all(
                "Item Price",
                filters={"item_code": ["in", child_codes], "price_list": self.setting.default_selling_price_list, "selling": 1},
                fields=["item_code", "price_list_rate"],
            )
            price_map = {p.item_code: p.price_list_rate for p in prices}

        list_pl = getattr(self.setting, "list_price_price_list", None)
        if list_pl:
            list_prices = frappe.get_all(
                "Item Price",
                filters={"item_code": ["in", child_codes], "price_list": list_pl, "selling": 1},
                fields=["item_code", "price_list_rate"],
            )
            list_price_map = {p.item_code: p.price_list_rate for p in list_prices}

        # Build variants and collect images + option values
        all_values = {attr: set() for attr in attribute_names}
        variant_payloads = []
        image_urls = []
        seen_images = set()

        # For updates: validate variant IDs against Medusa to avoid cross-product conflicts
        valid_variant_ids = {}
        sku_map_cache = {}
        if is_update:
            medusa_pid = self.item.get(PRODUCT_ID_FIELD)
            sku_map_cache = frappe.cache.get_value("medusa_sku_map") or {}
            if medusa_pid:
                for code in child_codes:
                    entry = sku_map_cache.get(code)
                    if entry and entry.get("product_id") == medusa_pid:
                        valid_variant_ids[code] = entry["variant_id"]

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

            selling = price_map.get(code, 0) or 0
            list_p = list_price_map.get(code, 0) or 0
            variant = self._make_variant_dict(
                child, currency,
                price_override=selling, list_price_override=list_p
            )
            variant["options"] = option_values

            # Variant-level metadata: same AI/SEO fields as product-level,
            # plus non-filterable ecommerce properties.
            v_meta = {}
            variant_ai_fields = {
                "ai_short_description": "short_description",
                "ai_long_description": "long_description",
                "ai_benefits": "benefits",
                "ai_applications": "applications",
                "ai_delivery_scope": "delivery_scope",
                "ai_seo_title": "seo_title",
                "ai_seo_description": "seo_description",
            }
            for erpnext_field, meta_key in variant_ai_fields.items():
                val = child.get(erpnext_field)
                if val:
                    v_meta[meta_key] = val
            seo_kw = child.get("seo_keywords")
            if seo_kw:
                v_meta["seo_keywords"] = seo_kw
            delivery_time = child.get("delivery_time")
            if delivery_time:
                v_meta["delivery_time"] = cstr(delivery_time).strip()
            # Add ecommerce properties as variant metadata
            for prop in ecom_props_by_item.get(code, []):
                if not prop.property_value:
                    continue
                # Filterable Properties go to product-level attributes, not variant metadata
                if prop.property_type == "Property" and prop.filterable:
                    continue
                key = f"custom_{prop.property_name}" if prop.property_type == "Custom Field" else prop.property_name
                v_meta[key] = cstr(prop.property_value).strip()
            if v_meta:
                variant["metadata"] = v_meta

            if is_update:
                vid = valid_variant_ids.get(code)
                if vid:
                    variant["id"] = vid
                elif code in sku_map_cache:
                    # SKU exists in a different Medusa product — mark for move
                    self._variants_to_move.append(sku_map_cache[code])

            # Set variant thumbnail directly in payload (no separate API call needed)
            if child.image:
                url = self._resolve_image_url(child.image)
                if url:
                    variant["thumbnail"] = url
                    variant_image_map[code] = url
                    if url not in seen_images:
                        image_urls.append(url)
                        seen_images.add(url)

            variant_payloads.append(variant)

        options = []
        for attr_name in attribute_names:
            values = sorted(all_values.get(attr_name, set()))
            if values:
                options.append({"title": attr_name, "values": values})

        if not options:
            options = [{"title": "Default", "values": ["Default"]}]

        return options, variant_payloads, image_urls, variant_image_map

    def _make_variant_dict(self, item_data, currency, price_override=None, list_price_override=None) -> dict:
        """Build a variant dict from item data.

        Uses UVP/list price as variant price if higher than selling price.
        The selling price will be added to a sale price list after creation.
        Stores sale price info in self._sale_prices for later sync.
        """
        code = item_data.item_code if hasattr(item_data, 'item_code') else item_data.get('item_code')
        selling_price = price_override if price_override is not None else self._get_selling_price(code)
        list_price = list_price_override if list_price_override is not None else self._get_list_price(code)

        # If UVP is higher, use it as variant price (becomes original_amount in storefront)
        # The selling price goes into a sale price list (becomes calculated_amount)
        if list_price and list_price > selling_price:
            variant_price = list_price
            self._sale_prices.append({"sku": code, "amount": erpnext_price_to_medusa(selling_price), "currency_code": currency})
        else:
            variant_price = selling_price

        is_dropship = getattr(item_data, "delivered_by_supplier", False)

        prices = [{"currency_code": currency, "amount": erpnext_price_to_medusa(variant_price)}]

        # Per-channel prices from channel-specific price lists
        for ch in (self.setting.sales_channels or []):
            if not ch.active:
                continue
            ch_price = self._get_channel_price(ch, code, currency)
            if ch_price is not None:
                self._channel_prices.append({
                    "sku": code,
                    "channel_id": ch.sales_channel_id,
                    "channel_name": ch.sales_channel_name or ch.short_code or ch.sales_channel_id,
                    "amount": erpnext_price_to_medusa(ch_price),
                    "currency_code": currency,
                })

        variant = {
            "title": item_data.item_name if hasattr(item_data, 'item_name') else item_data.get('item_name', ''),
            "sku": code,
            "manage_inventory": not is_dropship,
            "allow_backorder": bool(is_dropship),
            "prices": prices,
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
        text = _transliterate(title.lower())
        handle_base = re.sub(r'[^a-z0-9-]', '-', text)
        handle_base = re.sub(r'-+', '-', handle_base).strip('-')
        sku_slug = re.sub(r'[^a-z0-9-]', '-', _transliterate(self.item.item_code.lower()))
        sku_slug = re.sub(r'-+', '-', sku_slug).strip('-')
        return f"{handle_base}-{sku_slug}"

    def _get_medusa_category_ids(self, session=None, base_url=None) -> list:
        """Get all Medusa category IDs for this item.

        Sources:
        1. Item's primary item_group
        2. Website Item's additional item groups (multi-category)
        """
        cat_map = self._get_medusa_category_map(session=session, base_url=base_url)
        if not cat_map:
            return []

        groups = set()

        # Primary item group
        if self.item.item_group:
            groups.add(self.item.item_group)

        # Additional groups from Website Item
        wi_names = frappe.get_all("Website Item", filters={"item_code": self.item_code}, pluck="name")
        if wi_names:
            extra_groups = frappe.get_all(
                "Website Item Group",
                filters={"parenttype": "Website Item", "parent": ["in", wi_names]},
                pluck="item_group",
            )
            groups.update(extra_groups)

        return [cat_map[g] for g in groups if g in cat_map]

    def _get_medusa_category_map(self, session=None, base_url=None) -> dict:
        """Get {item_group_name: medusa_category_id} map, cached."""
        return _get_category_map(session=session, base_url=base_url)

    def _get_image_url(self):
        return self._resolve_image_url(self.item.image)

    def _get_all_image_urls(self) -> list:
        """Get all image URLs for this item (main image + file attachments)."""
        urls = []
        seen = set()

        # Main image first
        main = self._get_image_url()
        if main:
            urls.append(main)
            seen.add(main)

        # All image attachments
        attachments = frappe.get_all(
            "File",
            filters={
                "attached_to_doctype": "Item",
                "attached_to_name": self.item_code,
                "is_folder": 0,
            },
            fields=["file_url"],
            order_by="creation",
        )
        for f in attachments:
            if not is_image_url(f.file_url):
                continue
            url = self._resolve_image_url(f.file_url)
            if url and url not in seen:
                urls.append(url)
                seen.add(url)

        return urls

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

    def _get_ecommerce_properties(self) -> list:
        """Get ecommerce_properties with sync_to_medusa, falling back to first variant."""
        props = get_ecommerce_properties(self.item, "sync_to_medusa")
        if not props and self.item.has_variants:
            first_variant_name = frappe.db.get_value(
                "Item", {"variant_of": self.item_code, "disabled": 0}, "name")
            if first_variant_name:
                first_variant = frappe.get_doc("Item", first_variant_name)
                props = get_ecommerce_properties(first_variant, "sync_to_medusa")
        return props

    def _get_medusa_metadata(self) -> dict:
        """Non-filterable ecommerce properties -> product metadata."""
        metadata = {}
        for row in self._get_ecommerce_properties():
            if not row.property_value:
                continue
            if row.property_type == "Property" and getattr(row, "filterable", 0):
                continue
            key = f"custom_{row.property_name}" if row.property_type == 'Custom Field' else row.property_name
            metadata[key] = cstr(row.property_value).strip()
        return metadata

    def _collect_attribute_entries(self) -> list:
        """Collect (attr_name, value) tuples for this item's filterable attributes."""
        entries = []

        brand = getattr(self.item, "brand", None)
        if brand:
            entries.append(("Brand", cstr(brand).strip()))

        for row in self._get_ecommerce_properties():
            if row.property_value and row.property_type == "Property" and getattr(row, "filterable", 0):
                entries.append((row.property_name, cstr(row.property_value).strip()))

        return entries

    def _get_attribute_values(self, session=None, base_url=None) -> list:
        """Filterable ecommerce properties + brand -> medusa-product-attributes plugin.

        Ensures all needed attributes and possible values exist in Medusa
        (via batch endpoint), then returns list of {attribute_id, value}
        dicts for additional_data.values on the product payload.
        """
        entries = self._collect_attribute_entries()
        if not entries:
            return []

        attr_map = _ensure_attributes_exist(entries, session=session, base_url=base_url)
        return _resolve_attribute_values(entries, attr_map)

    def _get_price(self, price_list, item_code=None) -> float:
        if not price_list:
            return 0.0
        code = item_code or self.item_code
        return frappe.db.get_value("Item Price", {"item_code": code, "price_list": price_list, "selling": 1}, "price_list_rate") or 0.0

    def _get_channel_price(self, channel_row, item_code, currency):
        """Get channel-specific price. Returns adjusted price or None if no difference."""
        default_price = self._get_selling_price(item_code)

        if channel_row.price_list:
            ch_price = self._get_price(channel_row.price_list, item_code)
            if not ch_price:
                ch_price = default_price
        else:
            ch_price = default_price

        adjustment = channel_row.price_adjustment_percent or 0
        if adjustment:
            ch_price = ch_price * (1 + adjustment / 100)

        # Only return if different from default
        if abs(ch_price - default_price) > 0.01:
            return round(ch_price, 2)
        return None

    def _get_selling_price(self, item_code=None) -> float:
        return self._get_price(self.setting.default_selling_price_list, item_code)

    def _get_list_price(self, item_code=None) -> float:
        return self._get_price(getattr(self.setting, "list_price_price_list", None), item_code)

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

def _build_medusa_sku_map(session, base_url):
	"""Fetch all products from Medusa and build {sku: {product_id, variant_id, variant_skus}} map.

	Stores only IDs and SKU lists (not full product dicts) to keep Redis size small.
	Called once per sync. Cached for 10 minutes.
	"""
	cache_key = "medusa_sku_map"
	cached = frappe.cache.get_value(cache_key)
	if cached is not None:
		return cached

	products = medusa_request_all(
		session, base_url, API_PRODUCTS, "products",
		params={"fields": "id,+variants.id,+variants.sku", "limit": 200},
	)
	sku_map = {}
	for p in products:
		pid = p.get("id")
		if not pid:
			continue
		# {sku: variant_id} for all variants in this product
		product_skus = {v["sku"]: v["id"] for v in p.get("variants", []) if v.get("sku") and v.get("id")}
		for sku, vid in product_skus.items():
			sku_map[sku] = {
				"product_id": pid,
				"variant_id": vid,
				"product_skus": product_skus,
			}

	frappe.cache.set_value(cache_key, sku_map, expires_in_sec=600)
	frappe.logger("medusa").info(f"Built Medusa SKU map: {len(sku_map)} SKUs from {len(products)} products")
	return sku_map


def _reclassify_existing_products(session, base_url, create_batch, update_batch, variant_of_map):
	"""Check which create payloads already exist in Medusa (by variant SKU) and move them to updates.

	Returns (remaining_creates, extended_updates).
	"""
	if not create_batch:
		return create_batch, update_batch

	# Collect all SKUs from creates
	create_skus = set()
	for payload in create_batch:
		for v in payload.get("variants", []):
			if v.get("sku"):
				create_skus.add(v["sku"])

	if not create_skus:
		return create_batch, update_batch

	# Fetch full SKU map from Medusa (cached for this sync)
	sku_map = _build_medusa_sku_map(session, base_url)

	# Check which creates have SKUs that already exist
	remaining_creates = []
	for payload in create_batch:
		variant_skus = [v.get("sku") for v in payload.get("variants", []) if v.get("sku")]
		# If any variant SKU exists in Medusa, this product exists
		existing_entry = None
		for sku in variant_skus:
			if sku in sku_map:
				existing_entry = sku_map[sku]
				break

		if existing_entry:
			product_id = existing_entry["product_id"]
			product_skus = existing_entry["product_skus"]
			# Build a minimal product dict for _save_medusa_ids
			_save_medusa_ids(
				{"id": product_id, "variants": [{"sku": s, "id": vid} for s, vid in product_skus.items()]},
				variant_of_map=variant_of_map,
			)
			payload["id"] = product_id
			for v in payload.get("variants", []):
				vid = product_skus.get(v.get("sku"))
				if vid:
					v["id"] = vid
				else:
					v.pop("id", None)
			update_batch.append(payload)
		else:
			remaining_creates.append(payload)

	reclassified = len(create_batch) - len(remaining_creates)
	if reclassified:
		frappe.logger("medusa").info(f"Reclassified {reclassified} creates as updates (SKUs already exist in Medusa)")
		frappe.db.commit()

	return remaining_creates, update_batch


def _recover_existing_product(session, base_url, error, payload, variant_of_map, stats) -> bool:
    """Try to recover from 'already exists' error by looking up the product.

    Returns True if recovery succeeded (caller should continue), False otherwise.
    """
    if error.response is None or error.response.status_code != 400:
        return False
    try:
        resp = error.response.json()
        if "already exists" not in resp.get("message", ""):
            return False
        handle = payload.get("handle", "")
        if not handle:
            return False
        existing = medusa_request(session, base_url, "GET", API_PRODUCTS, params={"handle": handle, "fields": "id,variants"})
        for p in existing.get("products", []):
            _save_medusa_ids(p, variant_of_map=variant_of_map)
            stats["updated"] += 1
        return True
    except Exception:
        return False


def _find_item_code_by_handle(handle: str, variant_of_map: dict) -> str:
    """Find ERPNext item_code from a Medusa product handle (best-effort reverse lookup)."""
    if not handle:
        return ""
    # The handle ends with the slugified item_code — try to match
    for item_code in variant_of_map:
        if not variant_of_map.get(item_code):  # Only templates/simple items
            slug = re.sub(r'[^a-z0-9-]', '-', item_code.lower())
            slug = re.sub(r'-+', '-', slug).strip('-')
            if handle.endswith(slug):
                return item_code
    return ""


def _batch_assign_attributes(session, base_url, created_products: list, attr_value_maps: dict, variant_of_map: dict = None):
    """Assign attribute values to newly created products via plugin batch-assign endpoint.

    Uses POST /admin/plugin/attributes/batch-assign with the assignments format.
    """
    assignments = []
    for product in created_products:
        product_id = product.get("id")
        if not product_id:
            continue
        # Find the item_code's attribute values via variant SKU -> template lookup
        attr_values = _find_in_product_map(product, attr_value_maps, [], variant_of_map=variant_of_map)
        if not attr_values:
            continue
        for av in attr_values:
            assignments.append({
                "product_id": product_id,
                "attribute_id": av["attribute_id"],
                "value": av["value"],
            })

    if not assignments:
        return

    try:
        medusa_request(session, base_url, "POST", "/admin/plugin/attributes/batch-assign", json={"assignments": assignments})
    except Exception as e:
        frappe.log_error("Medusa Attributes", f"Batch attribute assign failed: {e}")

def _sku_to_variant_id(product: dict) -> dict:
    """Build {sku: variant_id} map from a Medusa product."""
    return {v["sku"]: v["id"] for v in product.get("variants", []) if v.get("sku") and v.get("id")}

def _upsert_price_list_entries(session, base_url, price_list_id: str, entries: list, log_label: str = "Medusa Prices"):
    """Replace price entries for given variants in a Medusa price list.

    Deletes existing entries for the affected variants, then creates new ones.
    """
    if not entries or not price_list_id:
        return

    try:
        variant_ids = {e["variant_id"] for e in entries}
        existing_pl = medusa_request(session, base_url, "GET", f"/admin/price-lists/{price_list_id}", params={"fields": "+prices"})
        existing_prices = existing_pl.get("price_list", {}).get("prices", [])
        delete_ids = [p["id"] for p in existing_prices if p.get("variant_id") in variant_ids]

        batch = {"create": entries}
        if delete_ids:
            batch["delete"] = delete_ids

        medusa_request(session, base_url, "POST", f"/admin/price-lists/{price_list_id}/prices/batch", json=batch)
    except Exception as e:
        frappe.log_error(log_label, f"Failed to upsert prices in {price_list_id}: {e}")

def _sync_channel_prices(session, base_url, product: dict, channel_prices: list):
    """Sync per-channel prices as Medusa Price Lists (type override)."""
    if not channel_prices:
        return
    _sync_channel_prices_batch(session, base_url, [(product, channel_prices)])

def _delete_variants(session, base_url, product_id: str, variants: list):
	"""Delete given variants from a Medusa product. Tolerates 4xx (e.g., order refs)."""
	if not variants:
		return
	deleted = 0
	for entry in variants:
		vid = entry["variant_id"]
		try:
			medusa_request(session, base_url, "DELETE",
				f"/admin/products/{product_id}/variants/{vid}")
			deleted += 1
		except Exception as e:
			frappe.logger("medusa").warning(
				f"Failed to delete disabled variant {entry.get('sku')} ({vid}): {e}"
			)
	if deleted:
		frappe.logger("medusa").info(f"Deleted {deleted} disabled variants from {product_id}")


def _delete_misplaced_variants(session, base_url, variants_to_move):
	"""Delete variants from wrong Medusa products so they can be re-created in the correct one."""
	if not variants_to_move:
		return
	deleted = 0
	for entry in variants_to_move:
		pid = entry["product_id"]
		vid = entry["variant_id"]
		try:
			medusa_request(session, base_url, "DELETE",
				f"/admin/products/{pid}/variants/{vid}")
			deleted += 1
		except Exception as e:
			frappe.logger("medusa").warning(f"Failed to delete misplaced variant {vid} from {pid}: {e}")
	if deleted:
		frappe.logger("medusa").info(f"Deleted {deleted} misplaced variants for re-assignment")


def _update_sync_progress(log_name, processed, total, stats):
	"""Update the Integration Log with current sync progress."""
	try:
		pct = int(processed / total * 100) if total else 0
		frappe.db.set_value(
			"Ecommerce Integration Log", log_name,
			{
				"status": "Queued",
				"message": f"Progress: {processed}/{total} items ({pct}%) — Created: {stats['created']}, Updated: {stats['updated']}, Errors: {stats['errors']}",
			},
			update_modified=True,
		)
		frappe.db.commit()
	except Exception:
		pass


def _sync_product_extras(session, base_url, products, variant_image_maps, sale_price_maps, channel_price_maps, variant_of_map):
	"""Sync variant images, sale prices, and channel prices for a list of products."""
	image_jobs = []
	sale_prices = []
	channel_prices = []
	for product in products:
		vim = _find_in_product_map(product, variant_image_maps, {}, variant_of_map=variant_of_map)
		if vim:
			image_jobs.extend(_collect_variant_image_jobs(product, vim))
		sp = _find_in_product_map(product, sale_price_maps, [], variant_of_map=variant_of_map)
		if sp:
			sale_prices.append((product, sp))
		cp = _find_in_product_map(product, channel_price_maps, [], variant_of_map=variant_of_map)
		if cp:
			channel_prices.append((product, cp))

	if image_jobs:
		_run_variant_image_jobs(session, base_url, image_jobs)
	if sale_prices:
		_sync_sale_prices_batch(session, base_url, sale_prices)
	if channel_prices:
		_sync_channel_prices_batch(session, base_url, channel_prices)


def _sync_channel_prices_batch(session, base_url, product_channel_prices: list):
    """Batch sync channel prices for multiple products.

    Groups all entries by channel, then syncs each channel's price list once.
    """
    if not product_channel_prices:
        return

    by_channel = {}
    for product, channel_prices in product_channel_prices:
        vid_map = _sku_to_variant_id(product)
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
        pl_id = _get_or_create_channel_price_list(session, base_url, ch_id, data["name"])
        if pl_id:
            _upsert_price_list_entries(session, base_url, pl_id, data["entries"], "Medusa Channel Prices")

def _get_or_create_channel_price_list(session, base_url, channel_id: str, channel_name: str) -> str:
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

def _save_medusa_ids(product: dict, variant_of_map: dict = None):
    """Save Medusa product/variant IDs back to ERPNext Items.

    For template products: saves product ID to template, variant IDs to child items.
    For simple products: saves both to the same item.

    Args:
        product: Medusa product dict with id and variants
        variant_of_map: optional {item_code: variant_of} lookup to avoid DB queries
    """
    product_id = product.get("id")
    if not product_id:
        return

    variants = product.get("variants", [])

    # Collect all updates: {item_code: {field: value}}
    updates = {}
    for variant in variants:
        sku = variant.get("sku")
        variant_id = variant.get("id")
        if not sku:
            continue

        if variant_of_map is not None:
            variant_of = variant_of_map.get(sku)
            if variant_of is None and sku not in variant_of_map:
                continue  # SKU not in our items
        else:
            if not frappe.db.exists("Item", sku):
                continue
            variant_of = frappe.db.get_value("Item", sku, "variant_of")

        template = variant_of or sku
        updates.setdefault(template, {})[PRODUCT_ID_FIELD] = product_id
        if variant_id:
            updates.setdefault(sku, {})[VARIANT_ID_FIELD] = variant_id

    # Bulk update: group by field values to minimize queries
    for item_code, fields in updates.items():
        frappe.db.set_value("Item", item_code, fields, update_modified=False)

def _collect_variant_image_jobs(product: dict, variant_image_map: dict) -> list:
    """Collect (product_id, image_id, variant_ids) tuples for parallel execution.

    Groups by image so each tuple becomes one API call via
    POST /products/{id}/images/{image_id}/variants/batch.
    """
    product_id = product.get("id")
    if not product_id:
        return []

    url_to_image_id = {}
    for img in product.get("images", []):
        if img.get("url") and img.get("id"):
            url_to_image_id[img["url"]] = img["id"]

    if not url_to_image_id:
        return []

    image_to_variants = {}
    for variant in product.get("variants", []):
        sku = variant.get("sku")
        variant_id = variant.get("id")
        if not sku or not variant_id or sku not in variant_image_map:
            continue
        image_url = variant_image_map[sku]
        image_id = url_to_image_id.get(image_url)
        if image_id:
            image_to_variants.setdefault(image_id, []).append(variant_id)

    return [(product_id, image_id, vids) for image_id, vids in image_to_variants.items()]


def _associate_variant_images(session, base_url, product: dict, variant_image_map: dict):
    """Associate product images with their specific variants (single-product, sequential).

    Used by single-product export. For bulk sync, use _run_variant_image_jobs.
    """
    for product_id, image_id, variant_ids in _collect_variant_image_jobs(product, variant_image_map):
        try:
            endpoint = f"{API_PRODUCTS}/{product_id}/images/{image_id}/variants/batch"
            medusa_request(session, base_url, "POST", endpoint, json={"add": variant_ids})
        except Exception as e:
            frappe.log_error("Medusa Variant Images", f"Failed for image {image_id}: {e}")


def _run_variant_image_jobs(session, base_url, jobs: list, max_workers=5):
    """Execute variant-image association jobs in parallel.

    Each job is a (product_id, image_id, variant_ids) tuple.
    Uses threads to overlap network I/O — the AdaptiveThrottle is thread-safe.
    """
    if not jobs:
        return

    def _do_one(product_id, image_id, variant_ids):
        endpoint = f"{API_PRODUCTS}/{product_id}/images/{image_id}/variants/batch"
        medusa_request(session, base_url, "POST", endpoint, json={"add": variant_ids})

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(_do_one, pid, iid, vids): (pid, iid)
            for pid, iid, vids in jobs
        }
        for future in as_completed(futures):
            pid, iid = futures[future]
            try:
                future.result()
            except Exception as e:
                frappe.log_error("Medusa Variant Images", f"Failed for product {pid} image {iid}: {e}")

def _sync_sale_prices(session, base_url, product: dict, sale_prices: list):
    """Add sale prices to a Medusa Price List for strikethrough display."""
    if not sale_prices:
        return
    _sync_sale_prices_batch(session, base_url, [(product, sale_prices)])

def _sync_sale_prices_batch(session, base_url, product_sale_prices: list):
    """Batch sync sale prices for multiple products in one API call."""
    all_entries = []
    for product, sale_prices in product_sale_prices:
        vid_map = _sku_to_variant_id(product)
        for sp in sale_prices:
            variant_id = vid_map.get(sp["sku"])
            if variant_id:
                all_entries.append({"variant_id": variant_id, "currency_code": sp["currency_code"], "amount": sp["amount"]})

    if not all_entries:
        return

    price_list_id = _get_or_create_sale_price_list(session, base_url)
    if price_list_id:
        _upsert_price_list_entries(session, base_url, price_list_id, all_entries, "Medusa Sale Prices")

def _get_or_create_sale_price_list(session, base_url) -> str:
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

def _find_in_product_map(product: dict, lookup_map: dict, default=None, variant_of_map: dict = None):
    """Find data in a map keyed by item_code, matching via variant SKU -> template lookup."""
    for variant in product.get("variants", []):
        sku = variant.get("sku")
        if not sku:
            continue
        if variant_of_map is not None:
            template = variant_of_map.get(sku)
        else:
            template = frappe.db.get_value("Item", sku, "variant_of")
        for key in [template, sku]:
            if key and key in lookup_map:
                return lookup_map[key]
    return default

SYNC_GENERATION_KEY = "medusa_full_sync_generation"
SYNC_RUNNING_KEY = "medusa_full_sync_running"

@frappe.whitelist()
def enqueue_full_sync(sync_categories=1, sync_products=1, sync_prices=1, sync_stock=0, batch_size=50, dry_run=0, validate_ids=0, index_strategy="full"):
	"""Enqueue a full product sync to Medusa as a background job.

	If a sync is already running, it is cancelled (after its current chunk
	finishes) and replaced by the new one. Uses a generation counter so
	each sync knows if a newer one has been enqueued.
	"""
	if not is_medusa_enabled():
		return {"success": False, "message": "Medusa integration is not enabled"}

	# Increment generation — any running sync with an older generation will stop
	generation = (frappe.cache.get_value(SYNC_GENERATION_KEY) or 0) + 1
	frappe.cache.set_value(SYNC_GENERATION_KEY, generation, expires_in_sec=7200)

	was_running = bool(frappe.cache.get_value(SYNC_RUNNING_KEY))
	message = "Previous sync cancelled. New sync enqueued." if was_running else "Full sync enqueued."

	frappe.enqueue(
		"ecommerce_integrations.medusa.product_export.run_full_sync",
		queue="long",
		timeout=3600,
		sync_generation=generation,
		sync_categories=int(sync_categories),
		sync_products=int(sync_products),
		sync_prices=int(sync_prices),
		sync_stock=int(sync_stock),
		batch_size=int(batch_size),
		dry_run=int(dry_run),
		validate_ids=int(validate_ids),
		index_strategy=index_strategy,
	)
	return {"success": True, "message": message}

def _is_sync_outdated(my_generation: int) -> bool:
	"""Check if a newer sync has been enqueued since this one started."""
	current = frappe.cache.get_value(SYNC_GENERATION_KEY) or 0
	return current > my_generation

def run_full_sync(sync_generation=0, sync_categories=1, sync_products=1, sync_prices=1, sync_stock=0, batch_size=50, dry_run=0, validate_ids=0, index_strategy="full"):
	"""Run a complete sync of all products from ERPNext to Medusa using batch API."""
	sync_generation = int(sync_generation) if sync_generation else 0

	# If a newer sync was already enqueued, skip this one
	if sync_generation and _is_sync_outdated(sync_generation):
		frappe.logger("medusa").info("Sync skipped — superseded by newer enqueue")
		return {"created": 0, "updated": 0, "errors": 0, "skipped": 0}

	frappe.cache.set_value(SYNC_RUNNING_KEY, True, expires_in_sec=3600)
	stats = {"created": 0, "updated": 0, "errors": 0, "skipped": 0, "error_details": []}
	batch_size = int(batch_size)

	log_name = create_medusa_log(
		request_type="Complete Sync",
		status="Queued",
		request_data={"batch_size": batch_size, "generation": sync_generation},
		response_data="Sync started",
	)

	try:
		_run_full_sync_inner(stats, batch_size, sync_generation, sync_categories, sync_products, sync_prices, sync_stock, dry_run, log_name=log_name, validate_ids=int(validate_ids), index_strategy=index_strategy)
	except Exception as e:
		frappe.log_error("Medusa Full Sync", f"Sync crashed: {e}")
		stats["errors"] += 1
		stats["error_details"].append(f"CRASH: {e}")
	finally:
		frappe.cache.delete_value(SYNC_RUNNING_KEY)
		# In finally so crashes mid-sync still re-index what was committed.
		if not dry_run and (stats["created"] + stats["updated"]) > 0:
			try:
				_trigger_index_sync_if_enabled(strategy=index_strategy)
			except Exception as e:
				frappe.log_error("Medusa Index Sync", f"Post-sync index trigger failed: {e}")
		error_details = stats.pop("error_details", [])
		status = "Error" if stats["errors"] > 0 else "Success"
		message = f"Created: {stats['created']}, Updated: {stats['updated']}, Errors: {stats['errors']}"
		traceback_text = "\n".join(error_details[-50:]) if error_details else ""  # Last 50 errors
		create_medusa_log(
			request_type="Complete Sync",
			status=status,
			request_data={"stats": stats},
			response_data=message,
			error=traceback_text,
		)
	return stats

def _run_full_sync_inner(stats, batch_size, sync_generation, sync_categories, sync_products, sync_prices, sync_stock, dry_run, log_name=None, validate_ids=0, index_strategy="full"):
	"""Inner sync logic, wrapped by run_full_sync for error handling."""
	from ecommerce_integrations.medusa.connection import get_medusa_session, _throttle
	_throttle.reset()

	if sync_products and not dry_run:
		try:
			from ecommerce_integrations.shopware6.export.property_handler import sync_surcharge_properties_batch
			surcharge_stats = sync_surcharge_properties_batch()
			frappe.logger("medusa").info(f"Surcharge property seed: {surcharge_stats}")
		except Exception as e:
			frappe.log_error("Medusa Full Sync", f"Surcharge property seed failed: {e}")

	if sync_categories:
		if log_name:
			_update_sync_progress(log_name, 0, 0, stats)
			frappe.db.set_value("Ecommerce Integration Log", log_name, "message", "Syncing categories...", update_modified=True)
			frappe.db.commit()
		try:
			cat_result = _sync_categories_to_medusa(dry_run=dry_run)
			frappe.logger("medusa").info(f"Category sync: {cat_result}")
			frappe.cache.delete_value("medusa_category_map")
		except Exception as e:
			frappe.log_error("Medusa Full Sync", f"Category sync failed: {e}")

	if sync_products or sync_prices:
		setting = frappe.get_cached_doc(SETTING_DOCTYPE)
		# Include disabled items so they get status=draft in Medusa (mirror ERPNext state)
		filters = {}
		if setting.category_sync_root:
			filters["item_group"] = ["descendants of (inclusive)", setting.category_sync_root]

		# Sync both simple items and templates
		items = frappe.get_all("Item", filters=filters, fields=["item_code", PRODUCT_ID_FIELD, "has_variants", "variant_of"], limit=0)

		# Exclude child variants (they are synced via their template)
		items = [i for i in items if not i.variant_of]

		if log_name:
			_update_sync_progress(log_name, 0, len(items), stats)

		if dry_run:
			stats["skipped"] = len(items)
		else:
			session, base_url = get_medusa_session()
			try:
				# Pre-build variant_of map to avoid per-SKU DB lookups
				all_item_codes = [i.item_code for i in items]
				all_child_variants = frappe.get_all(
					"Item",
					filters={"variant_of": ["in", all_item_codes]},
					fields=["item_code", "variant_of"],
				)
				variant_of_map = {v.item_code: v.variant_of for v in all_child_variants}
				# Also add non-variant items (variant_of = None)
				for i in items:
					if i.item_code not in variant_of_map:
						variant_of_map[i.item_code] = i.variant_of

				# Optionally validate existing medusa_product_ids against Medusa.
				# Skipped by default since ERPNext is the leading system.
				items_with_ids = [i for i in items if i.get(PRODUCT_ID_FIELD)]
				if validate_ids and items_with_ids:
					existing_products = medusa_request_all(session, base_url, API_PRODUCTS, "products", params={"fields": "id", "limit": 200})
					valid_ids = {p["id"] for p in existing_products}
					stale_count = 0
					for item in items:
						mid = item.get(PRODUCT_ID_FIELD)
						if mid and mid not in valid_ids:
							frappe.db.set_value("Item", item.item_code, {PRODUCT_ID_FIELD: None, VARIANT_ID_FIELD: None}, update_modified=False)
							item[PRODUCT_ID_FIELD] = None
							stale_count += 1
					if stale_count:
						frappe.db.commit()
						frappe.logger("medusa").info(f"Cleared {stale_count} stale medusa_product_ids")

				if log_name:
					frappe.db.set_value("Ecommerce Integration Log", log_name, "message",
						f"Warming caches for {len(items)} items...", update_modified=True)
					frappe.db.commit()
				# Pre-warm category and attribute caches once
				_get_category_map(session=session, base_url=base_url)
				_get_or_build_attribute_map(session=session, base_url=base_url)
				_build_medusa_sku_map(session, base_url)

				for chunk_start in range(0, len(items), batch_size):
					if sync_generation and _is_sync_outdated(sync_generation):
						frappe.logger("medusa").info(f"Sync cancelled (superseded) after {stats['created']} created, {stats['updated']} updated")
						break

					chunk = items[chunk_start:chunk_start + batch_size]
					create_batch = []
					update_batch = []

					variant_image_maps = {}
					sale_price_maps = {}
					channel_price_maps = {}
					# {item_code: [{"attribute_id": ..., "value": ...}]} for batch-assign after create
					attr_value_maps = {}

					# Pre-collect all attribute entries for this chunk and ensure
					# they exist in Medusa ONCE, to avoid duplicate possible_values
					chunk_attr_entries = []
					chunk_exporters = []
					for item_row in chunk:
						try:
							exporter = MedusaProductExporter(item_row.item_code)
							chunk_exporters.append((item_row, exporter))
							entries = exporter._collect_attribute_entries()
							chunk_attr_entries.extend(entries)
						except Exception as e:
							stats["errors"] += 1
							msg = f"Exporter init failed for {item_row.item_code}: {e}"
							frappe.log_error("Medusa Full Sync", msg)
							stats["error_details"].append(msg)

					# Ensure all attributes/values for the chunk in one call
					if chunk_attr_entries:
						_ensure_attributes_exist(chunk_attr_entries, session=session, base_url=base_url)

					for item_row, exporter in chunk_exporters:
						try:
							payload, vim = exporter._build_product_payload(is_update=bool(item_row.get(PRODUCT_ID_FIELD)), session=session, base_url=base_url)

							if vim:
								variant_image_maps[item_row.item_code] = vim
							if item_row.get(PRODUCT_ID_FIELD):
								payload["id"] = item_row.get(PRODUCT_ID_FIELD)
								update_batch.append(payload)
							else:
								create_batch.append(payload)
							if exporter._attribute_values:
								attr_value_maps[item_row.item_code] = exporter._attribute_values
							if exporter._channel_prices:
								channel_price_maps[item_row.item_code] = exporter._channel_prices
							if exporter._sale_prices:
								sale_price_maps[item_row.item_code] = exporter._sale_prices
						except Exception as e:
							stats["errors"] += 1
							msg = f"Payload build failed for {item_row.item_code}: {e}"
							frappe.log_error("Medusa Full Sync", msg)
							stats["error_details"].append(msg)

					# Re-classify creates as updates if product already exists in Medusa
					if create_batch:
						create_batch, update_batch = _reclassify_existing_products(
							session, base_url, create_batch, update_batch, variant_of_map)

					# Delete misplaced variants (SKU in wrong Medusa product) before batch
					all_variants_to_move = []
					for _, exporter in chunk_exporters:
						all_variants_to_move.extend(exporter._variants_to_move)
					if all_variants_to_move:
						_delete_misplaced_variants(session, base_url, all_variants_to_move)
					# Batch create/update ALL products
					created_products = []
					updated_products = []
					if create_batch or update_batch:
						try:
							batch_payload = {}
							if create_batch:
								batch_payload["create"] = create_batch
							if update_batch:
								batch_payload["update"] = update_batch

							result = medusa_request(session, base_url, "POST", API_PRODUCTS_BATCH, json=batch_payload, params={"fields": "id,handle,+variants.id,+variants.sku,+images.id,+images.url"})
							created_products = result.get("created", [])
							updated_products = result.get("updated", [])
							stats["updated"] += len(updated_products)
						except requests.exceptions.HTTPError as e:
							# Batch validation error — fall back to individual creates
							frappe.logger("medusa").warning(f"Batch failed ({e.response.status_code if e.response else '?'}), falling back to individual creates")
							all_payloads = list(create_batch)
							for payload in update_batch:
								payload_copy = {k: v for k, v in payload.items() if k != "id"}
								stale_id = payload.get("id")
								if stale_id:
									frappe.db.set_value("Item", {PRODUCT_ID_FIELD: stale_id}, {PRODUCT_ID_FIELD: None, VARIANT_ID_FIELD: None}, update_modified=False)
								all_payloads.append(payload_copy)
							for payload in all_payloads:
								# Add attribute data for single-create workflow hook
								item_code = _find_item_code_by_handle(payload.get("handle", ""), variant_of_map)
								if item_code and item_code in attr_value_maps:
									payload["additional_data"] = {"values": attr_value_maps[item_code]}
								try:
									result = medusa_request(session, base_url, "POST", API_PRODUCTS, json=payload)
									product = result.get("product", {})
									if product.get("id"):
										created_products.append(product)
								except requests.exceptions.HTTPError as single_e:
									if _recover_existing_product(session, base_url, single_e, payload, variant_of_map, stats):
										continue
									stats["errors"] += 1
									msg = f"Single create failed for {payload.get('handle', '?')}: {single_e}"
									stats["error_details"].append(msg)
								except Exception as single_e:
									stats["errors"] += 1
									msg = f"Single create failed for {payload.get('handle', '?')}: {single_e}"
									stats["error_details"].append(msg)
							frappe.db.commit()
						except Exception as e:
							stats["errors"] += len(create_batch) + len(update_batch)
							msg = f"Batch sync failed: {e}"
							frappe.log_error("Medusa Full Sync", msg)
							stats["error_details"].append(msg)

					# Process created products (works for both normal and retry path)
					if created_products:
						for product in created_products:
							_save_medusa_ids(product, variant_of_map=variant_of_map)
							stats["created"] += 1
						_sync_product_extras(session, base_url, created_products, variant_image_maps, sale_price_maps, channel_price_maps, variant_of_map)

					if updated_products:
						_sync_product_extras(session, base_url, updated_products, variant_image_maps, sale_price_maps, channel_price_maps, variant_of_map)

					# Batch-assign attributes — runs independently of batch create/update
					if attr_value_maps:
						try:
							all_products_for_attrs = list(created_products)
							for item_row, exporter in chunk_exporters:
								medusa_id = item_row.get(PRODUCT_ID_FIELD)
								if medusa_id and item_row.item_code in attr_value_maps:
									child_skus = [sku for sku, parent in variant_of_map.items()
									              if parent == item_row.item_code] if exporter.item.has_variants else [item_row.item_code]
									all_products_for_attrs.append({
										"id": medusa_id,
										"variants": [{"sku": sku} for sku in child_skus],
									})
							if all_products_for_attrs:
								_batch_assign_attributes(session, base_url, all_products_for_attrs, attr_value_maps, variant_of_map)

						except Exception as e:
							msg = f"Attribute assign failed: {e}"
							frappe.log_error("Medusa Full Sync", msg)
							stats["error_details"].append(msg)

					frappe.db.commit()

					# Update progress on the Integration Log
					if log_name:
						processed = min(chunk_start + batch_size, len(items))
						_update_sync_progress(log_name, processed, len(items), stats)

			finally:
				session.close()

	if sync_stock and not dry_run:
		try:
			from ecommerce_integrations.medusa.inventory import sync_inventory_to_medusa
			sync_inventory_to_medusa()
		except Exception as e:
			frappe.log_error("Medusa Full Sync", f"Inventory sync failed: {e}")

	frappe.logger("medusa").info(f"Full sync complete: {stats}")


def _trigger_index_sync_if_enabled(strategy: str = "full"):
	"""Trigger a Medusa index sync when the index engine feature flag is active.

	Bulk imports via the batch API don't emit the link events that the Medusa
	index engine needs.  Without a re-sync, newly created products may return
	404 on the storefront.

	Auto-detects the flag via GET /admin/feature-flags — no manual setting required.

	strategy:
	    "continue" — verarbeitet vorhandene PENDING-Entities
	    "full"     — markiert DONE/ERROR/PROCESSING als PENDING und re-syncht (default)
	    "reset"    — truncate + rebuild alles von vorne (worst case)
	"""
	if strategy not in ("continue", "full", "reset"):
		frappe.log_error("Medusa Index Sync", f"Invalid strategy: {strategy}")
		return
	try:
		_do_index_sync(strategy=strategy)
	except Exception as e:
		frappe.log_error("Medusa Index Sync", f"Index sync failed: {e}")


def _is_index_engine_enabled(session, base_url) -> bool:
	flags = medusa_request(session, base_url, "GET", API_FEATURE_FLAGS, timeout=10)
	return bool(flags.get("feature_flags", {}).get("index_engine"))


@temp_medusa_session
def _do_index_sync(session, base_url, strategy: str = "full"):
	if not _is_index_engine_enabled(session, base_url):
		return

	# TEMPORARY: built-in /admin/index/sync has a server-side bug
	# (Update-Transaction crashes with "already exists"). Use the custom
	# /admin/index-resync route which emits the index event cleanly.
	# Revert to the commented line once the upstream bug is fixed.
	medusa_request(session, base_url, "POST", "/admin/index-resync", params={"strategy": strategy}, timeout=120)
	# medusa_request(session, base_url, "POST", API_INDEX_SYNC, json={"strategy": strategy}, timeout=120)
	logger = frappe.logger("medusa")
	logger.info(f"Medusa index sync triggered (strategy={strategy})")

	try:
		details = medusa_request(session, base_url, "GET", API_INDEX_DETAILS, timeout=10)
		summary = _summarize_index_details(details)
		logger.info(f"Medusa index details: {summary}")
	except Exception as e:
		logger.warning(f"Could not fetch /admin/index/details: {e}")


def _summarize_index_details(details: dict) -> dict:
	"""Aggregate per-entity status into {counts, oldest_updated_at}."""
	from collections import Counter
	meta = details.get("metadata") or []
	counts = Counter(m.get("status") for m in meta)
	updated_ats = [m.get("updated_at") for m in meta if m.get("updated_at")]
	return {
		"entities": len(meta),
		"status_counts": dict(counts),
		"oldest_updated_at": min(updated_ats) if updated_ats else None,
	}


@frappe.whitelist()
def ensure_index_fresh(max_age_minutes: int = 60):
	"""Safety-net: trigger an index sync if last_synced_at is older than
	max_age_minutes. Covers worker-crash cases where the finally-block
	trigger in run_full_sync couldn't fire."""
	if not is_medusa_enabled():
		return
	try:
		_check_and_refresh_index(max_age_minutes=int(max_age_minutes))
	except Exception as e:
		frappe.log_error("Medusa Index Sync", f"Safety-net check failed: {e}")


@temp_medusa_session
def _check_and_refresh_index(session, base_url, max_age_minutes: int = 60):
	from datetime import datetime, timezone
	if not _is_index_engine_enabled(session, base_url):
		return
	details = medusa_request(session, base_url, "GET", API_INDEX_DETAILS, timeout=10)
	meta = details.get("metadata") or []

	# Any entity pending or running → never synced or currently syncing → trigger if pending
	pending = [m for m in meta if m.get("status") == "pending"]
	running = any(m.get("status") == "running" for m in meta)
	if running:
		return
	if pending:
		frappe.logger("medusa").info(f"Index has {len(pending)} pending entities, triggering sync")
		_trigger_index_sync_if_enabled(strategy="full")
		return

	# All entities done → check oldest updated_at as staleness signal
	updated_ats = [m.get("updated_at") for m in meta if m.get("updated_at")]
	if not updated_ats:
		return
	try:
		oldest = min(datetime.fromisoformat(u.replace("Z", "+00:00")) for u in updated_ats)
	except (ValueError, AttributeError):
		return
	age_minutes = (datetime.now(timezone.utc) - oldest).total_seconds() / 60
	if age_minutes > max_age_minutes:
		frappe.logger("medusa").info(
			f"Index stale (oldest {age_minutes:.0f}min > {max_age_minutes}min), triggering sync"
		)
		_trigger_index_sync_if_enabled(strategy="full")


def _enqueue_index_sync_debounced(strategy: str = "full", delay_seconds: int = 60):
	"""Debounced trigger for single-item sync path. deduplicate=True coalesces
	bursts of item updates — a second enqueue while the first job is still
	pending/running is dropped by RQ."""
	frappe.enqueue(
		"ecommerce_integrations.medusa.product_export._run_debounced_index_sync",
		queue="short",
		enqueue_after_commit=True,
		job_id=f"medusa_index_sync:{strategy}",
		deduplicate=True,
		strategy=strategy,
		delay_seconds=delay_seconds,
	)


def _run_debounced_index_sync(strategy: str = "full", delay_seconds: int = 60):
	import time
	time.sleep(max(0, int(delay_seconds)))
	_trigger_index_sync_if_enabled(strategy=strategy)

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

def deactivate_item_in_medusa(doc, method=None):
    """on_trash hook: set Medusa product status to draft (keep due to order refs)."""
    if not is_medusa_enabled():
        return
    medusa_id = doc.get(PRODUCT_ID_FIELD)
    if not medusa_id:
        return
    try:
        _set_product_draft(medusa_id)
    except Exception as e:
        frappe.log_error(f"Medusa deactivate failed: {doc.name}", str(e))


@temp_medusa_session
def _set_product_draft(session, base_url, medusa_id: str):
    medusa_request(session, base_url, "POST", f"{API_PRODUCTS}/{medusa_id}", json={"status": "draft"})


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
        _enqueue_index_sync_debounced(strategy="full")
    except Exception as e:
        frappe.log_error(f"Medusa product export failed: {doc.name}", str(e))


def sync_item_group_to_medusa(doc, method=None):
    """Sync a single Item Group change to Medusa categories (async)."""
    if not is_medusa_enabled():
        return
    frappe.cache.delete_value("medusa_category_map")
    frappe.enqueue(
        "ecommerce_integrations.medusa.product_export._enqueued_category_sync",
        queue="short",
        enqueue_after_commit=True,
    )


def _enqueued_category_sync():
    try:
        _sync_categories_to_medusa()
    except Exception as e:
        frappe.log_error(f"Medusa category sync failed", str(e))
