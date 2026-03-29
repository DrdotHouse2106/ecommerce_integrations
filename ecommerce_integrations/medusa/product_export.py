"""Export ERPNext Items to Medusa v2 as Products."""
import re
import time

BATCH_DELAY_SECONDS = 2

import frappe
import requests
from frappe.utils import cstr
from ecommerce_integrations.property_utils import get_ecommerce_properties
from ecommerce_integrations.medusa.connection import medusa_request, medusa_request_all, optional_session, temp_medusa_session
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
        self._sale_prices = []
        self._channel_prices = []
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

        if not is_update:
            brand = getattr(self.item, "brand", None)
            if hasattr(self.setting, 'get_channels_for_item'):
                channel_ids = self.setting.get_channels_for_item(self.item.item_group, brand)
            else:
                channel_ids = self.setting.get_active_sales_channel_ids() if hasattr(self.setting, 'get_active_sales_channel_ids') else []
            if channel_ids:
                payload["sales_channels"] = [{"id": ch_id} for ch_id in channel_ids]

        # Collect attribute values (for post-create batch-assign)
        self._attribute_values = self._get_attribute_values(session=session, base_url=base_url)

        meta = self._build_metadata()
        if meta:
            payload["metadata"] = meta

        variant_image_map = {}

        if not is_update:
            if self.item.has_variants:
                options, variants, image_urls, variant_image_map = self._build_template_variants(currency)
                payload["options"] = options
                payload["variants"] = variants
            else:
                image_urls = self._get_all_image_urls()
                payload["options"] = [{"title": "Default", "values": ["Default"]}]
                payload["variants"] = [self._build_single_variant_payload(currency)]
        else:
            image_urls = self._get_all_image_urls()

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

    def _build_template_variants(self, currency) -> tuple:
        """Build options, variants, and image URLs for a template item.

        Uses batch queries to avoid N+1: fetches all child items, their
        attributes, prices, and images in bulk.

        Returns (options, variant_payloads, image_urls).
        """
        attribute_names = [a.attribute for a in self.item.get("attributes", [])] or ["Default"]

        # {sku: image_url} for post-create variant-image association
        variant_image_map = {}

        # Batch fetch all child data in 3 queries instead of N * get_doc
        child_codes_rows = frappe.get_all(
            "Item",
            filters={"variant_of": self.item_code, "disabled": 0},
            fields=["item_code", "item_name", "weight_per_unit", "item_height",
                     "item_width", "item_length", "customs_tariff_number",
                     "country_of_origin", "delivered_by_supplier", "image",
                     "ai_short_description", "ai_seo_title", "ai_seo_description"],
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

            # Variant-level metadata (AI descriptions specific to this variant)
            v_meta = {}
            if child.get("ai_short_description"):
                v_meta["short_description"] = child.ai_short_description
            if child.get("ai_seo_title"):
                v_meta["seo_title"] = child.ai_seo_title
            if child.get("ai_seo_description"):
                v_meta["seo_description"] = child.ai_seo_description
            if v_meta:
                variant["metadata"] = v_meta

            variant_payloads.append(variant)

            # Collect variant image and track SKU→URL mapping
            if child.image:
                url = self._resolve_image_url(child.image)
                if url:
                    variant_image_map[code] = url
                    if url not in seen_images:
                        image_urls.append(url)
                        seen_images.add(url)

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
            if not f.file_url:
                continue
            ext = (f.file_url.rsplit(".", 1)[-1] if "." in f.file_url else "").lower()
            if ext not in ("jpg", "jpeg", "png", "webp", "avif", "gif"):
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

    def _get_medusa_metadata(self) -> dict:
        """Non-filterable ecommerce properties -> metadata."""
        metadata = {}
        for row in get_ecommerce_properties(self.item, "sync_to_medusa"):
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

        for row in get_ecommerce_properties(self.item, "sync_to_medusa"):
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


_UMLAUT_MAP = str.maketrans({
    "ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss",
    "Ä": "ae", "Ö": "oe", "Ü": "ue",
    "é": "e", "è": "e", "ê": "e", "à": "a", "â": "a",
    "ô": "o", "î": "i", "ç": "c", "ñ": "n",
})


def _transliterate(text: str) -> str:
    """Replace umlauts and accented chars with ASCII equivalents."""
    return text.translate(_UMLAUT_MAP)


def _resolve_attribute_values(entries: list, attr_map: dict) -> list:
    """Resolve (attr_name, value) entries to [{attribute_id, value}] using attr_map."""
    result = []
    for prop_name, prop_value in entries:
        attr_entry = attr_map.get(prop_name)
        if attr_entry and attr_entry.get("id"):
            result.append({"attribute_id": attr_entry["id"], "value": prop_value})
    return result


def _get_category_map(session=None, base_url=None) -> dict:
    """Get {item_group_name: medusa_category_id} map, cached."""
    from ecommerce_integrations.medusa.constants import API_CATEGORIES

    cache_key = "medusa_category_map"
    cat_map = frappe.cache.get_value(cache_key)
    if cat_map is not None:
        return cat_map

    with optional_session(session, base_url) as (s, url):
        try:
            categories = medusa_request_all(s, url, API_CATEGORIES, "product_categories")
            cat_map = {c["name"]: c["id"] for c in categories}
            frappe.cache.set_value(cache_key, cat_map, expires_in_sec=300)
        except Exception:
            cat_map = {}
    return cat_map


def _get_or_build_attribute_map(session=None, base_url=None) -> dict:
    """Get {attr_name: {"id": attr_id, "values": {val: pv_id}}} map, cached."""
    cache_key = "medusa_attribute_map"
    attr_map = frappe.cache.get_value(cache_key)
    if attr_map is not None:
        return attr_map

    with optional_session(session, base_url) as (s, url):
        try:
            attrs = medusa_request_all(s, url, "/admin/plugin/attributes", "attributes")
            attr_map = {}
            for a in attrs:
                attr_map[a["name"]] = {
                    "id": a["id"],
                    "values": {pv["value"]: pv["id"] for pv in a.get("possible_values", [])},
                }
            frappe.cache.set_value(cache_key, attr_map, expires_in_sec=300)
        except Exception:
            attr_map = {}
    return attr_map


def _ensure_attributes_exist(entries: list, session=None, base_url=None) -> dict:
    """Ensure all attributes and their possible values exist in Medusa.

    Uses the batch endpoint POST /admin/plugin/attributes/batch to create
    missing attributes with all their values in a single request.

    Args:
        entries: list of (attr_name, value) tuples
        session: optional existing requests.Session to reuse
        base_url: optional base URL (required if session is provided)

    Returns:
        attr_map: {attr_name: {"id": attr_id, "values": {val: pv_id}}}
    """
    attr_map = _get_or_build_attribute_map(session=session, base_url=base_url)

    # Collect attributes with missing values
    to_create = {}  # {name: set(values)}
    for name, value in entries:
        existing = attr_map.get(name)
        if not existing:
            to_create.setdefault(name, set()).add(value)
        elif value not in existing.get("values", {}):
            to_create.setdefault(name, set()).add(value)

    if not to_create:
        return attr_map

    # Send everything as "create" — the endpoint auto-merges new values
    # into existing attributes (no separate update path needed)
    create_payloads = []
    for name, values in to_create.items():
        handle = _transliterate(name.lower())
        handle = re.sub(r'[^a-z0-9-]', '-', handle)
        handle = re.sub(r'-+', '-', handle).strip('-')
        # Start rank after existing values so merged values get ascending ranks
        existing = attr_map.get(name)
        rank_start = len(existing.get("values", {})) if existing else 0
        create_payloads.append({
            "name": name,
            "handle": handle,
            "is_filterable": True,
            "is_variant_defining": False,
            "ui_component": "select",
            "possible_values": [{"value": v, "rank": rank_start + i} for i, v in enumerate(sorted(values))],
        })

    with optional_session(session, base_url) as (s, url):
        try:
            result = medusa_request(s, url, "POST", "/admin/plugin/attributes/batch", json={"create": create_payloads})

            # Update cache from created attributes (full possible_values in response)
            for attr in result.get("created", []):
                attr_map[attr["name"]] = {
                    "id": attr["id"],
                    "values": {pv["value"]: pv["id"] for pv in attr.get("possible_values", [])},
                }

            # For merged attributes, invalidate cache entry so it's re-fetched
            # (merged response only has new_values strings, not full possible_values with IDs)
            if result.get("merged"):
                frappe.cache.delete_value("medusa_attribute_map")
                attr_map = _get_or_build_attribute_map(session=s, base_url=url)
            else:
                # Mark sent values as present to prevent re-sending within same sync
                for name, values in to_create.items():
                    entry = attr_map.get(name)
                    if entry:
                        for v in values:
                            entry["values"].setdefault(v, "pending")
                frappe.cache.set_value("medusa_attribute_map", attr_map, expires_in_sec=300)
        except Exception as e:
            frappe.log_error("Medusa Attributes", f"Batch attribute sync failed: {e}")

    return attr_map


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


def _associate_variant_images(session, base_url, product: dict, variant_image_map: dict):
    """Associate product images with their specific variants in Medusa.

    After product creation, all images are on the product level. This
    function maps each variant's image URL to its Medusa image ID and
    calls the variant-image batch endpoint to associate them.

    Args:
        variant_image_map: {sku: image_url} from _build_template_variants
    """
    product_id = product.get("id")
    if not product_id:
        return

    # Build {url: image_id} from created product
    url_to_image_id = {}
    for img in product.get("images", []):
        if img.get("url") and img.get("id"):
            url_to_image_id[img["url"]] = img["id"]

    if not url_to_image_id:
        return

    # Group by image: {image_id: [variant_ids]} — allows one API call per
    # unique image instead of one per variant (more efficient when variants
    # share images). Uses the image-based endpoint: POST /products/{id}/images/{image_id}/variants/batch
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

    for image_id, variant_ids in image_to_variants.items():
        try:
            endpoint = f"{API_PRODUCTS}/{product_id}/images/{image_id}/variants/batch"
            medusa_request(session, base_url, "POST", endpoint, json={"add": variant_ids})
        except Exception as e:
            frappe.log_error("Medusa Variant Images", f"Failed for image {image_id}: {e}")


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
def enqueue_full_sync(sync_categories=1, sync_products=1, sync_prices=1, sync_stock=0, batch_size=50, dry_run=0):
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
	)
	return {"success": True, "message": message}


def _is_sync_outdated(my_generation: int) -> bool:
	"""Check if a newer sync has been enqueued since this one started."""
	current = frappe.cache.get_value(SYNC_GENERATION_KEY) or 0
	return current > my_generation


def run_full_sync(sync_generation=0, sync_categories=1, sync_products=1, sync_prices=1, sync_stock=0, batch_size=50, dry_run=0):
	"""Run a complete sync of all products from ERPNext to Medusa using batch API."""
	sync_generation = int(sync_generation) if sync_generation else 0

	# If a newer sync was already enqueued, skip this one
	if sync_generation and _is_sync_outdated(sync_generation):
		frappe.logger("medusa").info("Sync skipped — superseded by newer enqueue")
		return {"created": 0, "updated": 0, "errors": 0, "skipped": 0}

	frappe.cache.set_value(SYNC_RUNNING_KEY, True, expires_in_sec=3600)
	stats = {"created": 0, "updated": 0, "errors": 0, "skipped": 0}
	batch_size = int(batch_size)

	try:
		_run_full_sync_inner(stats, batch_size, sync_generation, sync_categories, sync_products, sync_prices, sync_stock, dry_run)
	except Exception as e:
		frappe.log_error("Medusa Full Sync", f"Sync crashed: {e}")
		stats["errors"] += 1
	finally:
		frappe.cache.delete_value(SYNC_RUNNING_KEY)
		status = "Error" if stats["errors"] > 0 else "Success"
		message = f"Created: {stats['created']}, Updated: {stats['updated']}, Errors: {stats['errors']}"
		create_medusa_log(
			request_type="Complete Sync",
			status=status,
			request_data={"stats": stats},
			response_data=message,
		)
	return stats


def _run_full_sync_inner(stats, batch_size, sync_generation, sync_categories, sync_products, sync_prices, sync_stock, dry_run):
	"""Inner sync logic, wrapped by run_full_sync for error handling."""
	from ecommerce_integrations.medusa.connection import get_medusa_session

	if sync_categories:
		try:
			cat_result = _sync_categories_to_medusa(dry_run=dry_run)
			frappe.logger("medusa").info(f"Category sync: {cat_result}")
			# Invalidate category cache so product sync picks up newly created categories
			frappe.cache.delete_value("medusa_category_map")
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
				# Pre-build variant_of map to avoid per-SKU DB lookups
				all_item_codes = [i.item_code for i in items]
				all_child_variants = frappe.get_all(
					"Item",
					filters={"variant_of": ["in", all_item_codes], "disabled": 0},
					fields=["item_code", "variant_of"],
				)
				variant_of_map = {v.item_code: v.variant_of for v in all_child_variants}
				# Also add non-variant items (variant_of = None)
				for i in items:
					if i.item_code not in variant_of_map:
						variant_of_map[i.item_code] = i.variant_of

				# Pre-warm category and attribute caches once
				_get_category_map(session=session, base_url=base_url)
				_get_or_build_attribute_map(session=session, base_url=base_url)

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
							frappe.log_error("Medusa Full Sync", f"Exporter init failed for {item_row.item_code}: {e}")

					# Ensure all attributes/values for the chunk in one call
					if chunk_attr_entries:
						_ensure_attributes_exist(chunk_attr_entries, session=session, base_url=base_url)

					for item_row, exporter in chunk_exporters:
						try:
							payload, vim = exporter._build_product_payload(is_update=bool(item_row.get(PRODUCT_ID_FIELD)), session=session, base_url=base_url)

							if item_row.get(PRODUCT_ID_FIELD):
								payload["id"] = item_row.get(PRODUCT_ID_FIELD)
								update_batch.append(payload)
							else:
								create_batch.append(payload)
								if vim:
									variant_image_maps[item_row.item_code] = vim
							if exporter._attribute_values:
								attr_value_maps[item_row.item_code] = exporter._attribute_values
							if exporter._channel_prices:
								channel_price_maps[item_row.item_code] = exporter._channel_prices
							if exporter._sale_prices:
								sale_price_maps[item_row.item_code] = exporter._sale_prices
						except Exception as e:
							stats["errors"] += 1
							frappe.log_error("Medusa Full Sync", f"Payload build failed for {item_row.item_code}: {e}")

					# Batch create/update ALL products
					created_products = []
					if create_batch or update_batch:
						try:
							batch_payload = {}
							if create_batch:
								batch_payload["create"] = create_batch
							if update_batch:
								batch_payload["update"] = update_batch

							result = medusa_request(session, base_url, "POST", API_PRODUCTS_BATCH, json=batch_payload)
							created_products = result.get("created", [])
							stats["updated"] += len(result.get("updated", []))
						except requests.exceptions.HTTPError as e:
							resp_body = ""
							if e.response is not None:
								try:
									resp_body = e.response.text[:500]
								except Exception:
									pass
							if e.response is not None and e.response.status_code in (400, 404) and update_batch and not create_batch:
								# Only retry when the error is likely from stale update IDs
								# (if create_batch was also present, the error could be from those)
								frappe.logger("medusa").warning(f"Batch update failed ({e.response.status_code}), clearing stale IDs and retrying as create")
								retry_batch = []
								for payload in update_batch:
									stale_id = payload.pop("id", None)
									if stale_id:
										frappe.db.set_value("Item", {PRODUCT_ID_FIELD: stale_id}, {PRODUCT_ID_FIELD: None, VARIANT_ID_FIELD: None}, update_modified=False)
									retry_batch.append(payload)
								try:
									time.sleep(BATCH_DELAY_SECONDS)
									result = medusa_request(session, base_url, "POST", API_PRODUCTS_BATCH, json={"create": retry_batch})
									created_products = result.get("created", [])
								except Exception as retry_e:
									stats["errors"] += len(retry_batch)
									frappe.log_error("Medusa Full Sync", f"Retry batch failed: {retry_e}")
							else:
								stats["errors"] += len(create_batch) + len(update_batch)
								frappe.log_error("Medusa Full Sync", f"Batch sync failed ({e.response.status_code if e.response is not None else '?'}): {resp_body or e}")
						except Exception as e:
							stats["errors"] += len(create_batch) + len(update_batch)
							frappe.log_error("Medusa Full Sync", f"Batch sync failed: {e}")

					# Process created products (works for both normal and retry path)
					if created_products:
						chunk_sale_prices = []
						chunk_channel_prices = []
						all_variant_images = []
						for product in created_products:
							_save_medusa_ids(product, variant_of_map=variant_of_map)
							stats["created"] += 1
							vim = _find_in_product_map(product, variant_image_maps, {}, variant_of_map=variant_of_map)
							if vim:
								all_variant_images.append((product, vim))
							sp = _find_in_product_map(product, sale_price_maps, [], variant_of_map=variant_of_map)
							if sp:
								chunk_sale_prices.append((product, sp))
							cp = _find_in_product_map(product, channel_price_maps, [], variant_of_map=variant_of_map)
							if cp:
								chunk_channel_prices.append((product, cp))

						time.sleep(BATCH_DELAY_SECONDS)

						for product, vim in all_variant_images:
							_associate_variant_images(session, base_url, product, vim)
						if all_variant_images:
							time.sleep(BATCH_DELAY_SECONDS)

						if chunk_sale_prices:
							_sync_sale_prices_batch(session, base_url, chunk_sale_prices)
							time.sleep(BATCH_DELAY_SECONDS)

						if chunk_channel_prices:
							_sync_channel_prices_batch(session, base_url, chunk_channel_prices)
							time.sleep(BATCH_DELAY_SECONDS)

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
								time.sleep(BATCH_DELAY_SECONDS)
						except Exception as e:
							frappe.log_error("Medusa Full Sync", f"Attribute assign failed: {e}")

					frappe.db.commit()
					if create_batch or update_batch:
						time.sleep(BATCH_DELAY_SECONDS)
			finally:
				session.close()

	if sync_stock and not dry_run:
		try:
			from ecommerce_integrations.medusa.inventory import sync_inventory_to_medusa
			sync_inventory_to_medusa()
		except Exception as e:
			frappe.log_error("Medusa Full Sync", f"Inventory sync failed: {e}")

	frappe.logger("medusa").info(f"Full sync complete: {stats}")


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
