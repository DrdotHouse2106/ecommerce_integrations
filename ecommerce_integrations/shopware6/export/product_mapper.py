"""
Shopware 6 Product Mapper

Maps ERPNext Item fields to Shopware product payload.
Handles price calculation, tax rates, and SEO fields.
"""

from typing import Any, Dict, List, Optional

import frappe
from frappe.utils import flt

from ecommerce_integrations.shopware6.constants import ITEM_SELLING_RATE_FIELD
# Note: Internal functions - client is passed from caller
from ecommerce_integrations.shopware6.base.cache_manager import get_cache
from ecommerce_integrations.shopware6.export.utils import generate_uuid


def get_tax_id_by_rate(client, tax_rate: float = 19.0) -> Optional[str]:
    """
    Get the Shopware tax ID for a given tax rate.

    Args:
        client: Shopware API client
        tax_rate: Tax rate (e.g., 19.0, 7.0, 0.0)

    Returns:
        Shopware tax ID if found, None otherwise
    """
    cache = get_cache()
    tax_rate = round(tax_rate, 2)

    cached_id = cache.get("tax", str(tax_rate))
    if cached_id:
        return cached_id

    try:
        response = client.request_post(
            "search/tax",
            {"filter": [{"type": "equals", "field": "taxRate", "value": tax_rate}], "limit": 1}
        )
        taxes = response.get("data", [])

        if taxes:
            tax_id = taxes[0]["id"]
            cache.set("tax", str(tax_rate), tax_id)
            return tax_id

        # Try closest standard rate
        standard_rates = [19.0, 7.0, 0.0]
        closest = min(standard_rates, key=lambda x: abs(x - tax_rate))

        if closest != tax_rate:
            response = client.request_post(
                "search/tax",
                {"filter": [{"type": "equals", "field": "taxRate", "value": closest}], "limit": 1}
            )
            taxes = response.get("data", [])
            if taxes:
                tax_id = taxes[0]["id"]
                cache.set("tax", str(tax_rate), tax_id)
                return tax_id

        # Ultimate fallback
        response = client.request_post("search/tax", {"limit": 1})
        taxes = response.get("data", [])
        if taxes:
            tax_id = taxes[0]["id"]
            cache.set("tax", str(tax_rate), tax_id)
            return tax_id

        return None

    except Exception as e:
        frappe.log_error(f"Failed to get tax ID for rate {tax_rate}%: {e}")
        return None


def get_or_create_delivery_time(client, delivery_time_name: str) -> Optional[str]:
    """
    Get existing or create new Delivery Time in Shopware.

    Args:
        client: Shopware API client
        delivery_time_name: Name of the delivery time

    Returns:
        Shopware delivery time ID
    """
    import re

    cache = get_cache()
    cached_id = cache.get("delivery_time", delivery_time_name)
    if cached_id:
        return cached_id

    try:
        response = client.request_post(
            "search/delivery-time",
            {"filter": [{"type": "equals", "field": "name", "value": delivery_time_name}]}
        )
        times = response.get("data", [])

        if times:
            dt_id = times[0]["id"]
            cache.set("delivery_time", delivery_time_name, dt_id)
            return dt_id

        # Parse delivery time
        name_lower = delivery_time_name.lower()

        if "sofort" in name_lower or "lagernd" in name_lower:
            min_val, max_val, unit = 1, 3, 'day'
        else:
            match_range = re.search(r'(\d+)\s*[-–]\s*(\d+)', delivery_time_name)
            match_single = re.search(r'(\d+)', delivery_time_name)

            if match_range:
                min_val = int(match_range.group(1))
                max_val = int(match_range.group(2))
            elif match_single:
                val = int(match_single.group(1))
                min_val = max_val = val
            else:
                min_val, max_val = 1, 3

            if 'woche' in name_lower or 'week' in name_lower:
                unit = 'week'
            elif 'monat' in name_lower or 'month' in name_lower:
                unit = 'month'
            elif 'jahr' in name_lower or 'year' in name_lower:
                unit = 'year'
            else:
                unit = 'day'

        dt_id = generate_uuid(f"delivery_time_{delivery_time_name}")
        client.request_post("delivery-time", {
            "id": dt_id,
            "name": delivery_time_name,
            "min": min_val,
            "max": max_val,
            "unit": unit,
        })
        cache.set("delivery_time", delivery_time_name, dt_id)
        return dt_id

    except Exception as e:
        frappe.log_error(f"Failed to get/create DeliveryTime {delivery_time_name}: {e}")
        return None


def get_or_create_manufacturer(client, manufacturer_name: str) -> Optional[str]:
    """
    Get existing or create new Manufacturer in Shopware.

    Args:
        client: Shopware API client
        manufacturer_name: Name of the manufacturer

    Returns:
        Shopware manufacturer ID
    """
    if not manufacturer_name:
        return None

    cache = get_cache()
    cached_id = cache.get("manufacturer", manufacturer_name)
    if cached_id:
        return cached_id

    try:
        response = client.request_post(
            "search/product-manufacturer",
            {"filter": [{"type": "equals", "field": "name", "value": manufacturer_name}]}
        )
        manufacturers = response.get("data", [])

        if manufacturers:
            mfr_id = manufacturers[0]["id"]
            cache.set("manufacturer", manufacturer_name, mfr_id)
            return mfr_id

        mfr_id = generate_uuid(f"manufacturer_{manufacturer_name}")
        client.request_post("product-manufacturer", {"id": mfr_id, "name": manufacturer_name})
        cache.set("manufacturer", manufacturer_name, mfr_id)
        return mfr_id

    except Exception as e:
        frappe.log_error(f"Failed to get/create Manufacturer {manufacturer_name}: {e}")
        return None


def get_cached_currency_id(client, currency_code: str = "EUR") -> Optional[str]:
    """
    Get currency ID from cache or fetch from Shopware API.

    Args:
        client: Shopware API client
        currency_code: ISO currency code

    Returns:
        Currency ID
    """
    cache = get_cache()
    cached_id = cache.get_currency_id(currency_code)
    if cached_id:
        return cached_id

    try:
        response = client.request_post(
            "search/currency",
            {"filter": [{"type": "equals", "field": "isoCode", "value": currency_code}], "limit": 1}
        )
        currencies = response.get("data", [])
        if currencies:
            currency_id = currencies[0]["id"]
            cache.set_currency_id(currency_code, currency_id)
            return currency_id
    except Exception as e:
        frappe.log_error(f"Failed to get currency ID for {currency_code}: {e}")

    return None


def get_cached_sales_channel_id(client) -> Optional[str]:
    """
    Get default sales channel ID from cache or fetch from API.

    Args:
        client: Shopware API client

    Returns:
        Sales channel ID
    """
    cache = get_cache()
    cached_id = cache.get_sales_channel_id()
    if cached_id:
        return cached_id

    try:
        response = client.request_get("sales-channel")
        sales_channels = response.get("data", [])
        if sales_channels:
            sc_id = sales_channels[0]["id"]
            cache.set_sales_channel_id(sc_id)
            return sc_id
    except Exception as e:
        frappe.log_error(f"Failed to get sales channel ID: {e}")

    return None


def map_erpnext_item_to_shopware(erpnext_item) -> Dict[str, Any]:
    """
    Map ERPNext Item fields to Shopware product payload.

    Args:
        erpnext_item: ERPNext Item document

    Returns:
        Dict suitable for Shopware product API
    """
    # Description priority
    description = (
        getattr(erpnext_item, 'ai_long_description', None) or
        getattr(erpnext_item, 'web_long_description', None) or
        erpnext_item.description or
        erpnext_item.item_name
    )

    payload = {
        "productNumber": erpnext_item.item_code,
        "name": erpnext_item.item_name,
        "description": description,
        "active": not erpnext_item.disabled,
        "stock": 0,
        "isCloseout": False,
        "translations": {
            "de-DE": {
                "name": erpnext_item.item_name,
                "description": description
            }
        }
    }

    # Price
    price = flt(erpnext_item.get(ITEM_SELLING_RATE_FIELD) or 0)

    # Fallback to Item Price
    if price <= 0:
        try:
            item_price = frappe.db.get_value(
                "Item Price",
                {"item_code": erpnext_item.name, "selling": 1, "price_list_rate": [">", 0]},
                "price_list_rate",
                order_by="price_list_rate desc"
            )
            if item_price:
                price = flt(item_price)
        except Exception:
            pass

    # For templates, find lowest variant price
    if price <= 0 and getattr(erpnext_item, "has_variants", False):
        try:
            result = frappe.db.sql("""
                SELECT price_list_rate FROM `tabItem Price`
                WHERE item_code IN (SELECT name FROM `tabItem` WHERE variant_of = %s AND disabled = 0)
                AND price_list_rate > 0
                ORDER BY price_list_rate ASC LIMIT 1
            """, erpnext_item.name)
            if result and result[0][0]:
                price = flt(result[0][0])
            else:
                price = 0.01
        except Exception:
            price = 0.01

    # Tax rate
    tax_rate = 19.0
    if hasattr(erpnext_item, 'taxes') and erpnext_item.taxes:
        for tax_row in erpnext_item.taxes:
            if tax_row.item_tax_template:
                template_rate = frappe.db.get_value(
                    "Item Tax Template Detail",
                    {"parent": tax_row.item_tax_template},
                    "tax_rate"
                )
                if template_rate:
                    tax_rate = flt(template_rate)
                    break

    gross_price = price if price > 0 else 0.01
    net_price = price if price > 0 else 0.01

    if price > 0:
        gross_price = round(net_price * (1 + tax_rate / 100), 2)

    payload["price"] = [{
        "currencyId": None,
        "gross": gross_price,
        "net": net_price,
        "linked": False,
    }]
    payload["_tax_rate"] = tax_rate

    # Weight
    weight = flt(erpnext_item.weight_per_unit or 0)
    if weight > 0:
        payload["weight"] = weight

    # Dimensions (cm to mm)
    if hasattr(erpnext_item, 'item_height') and erpnext_item.item_height:
        payload["height"] = flt(erpnext_item.item_height) * 10
    if hasattr(erpnext_item, 'item_width') and erpnext_item.item_width:
        payload["width"] = flt(erpnext_item.item_width) * 10
    if hasattr(erpnext_item, 'item_length') and erpnext_item.item_length:
        payload["length"] = flt(erpnext_item.item_length) * 10

    # SEO fields
    seo_title = getattr(erpnext_item, 'ai_seo_title', None) or getattr(erpnext_item, 'seo_title', None)
    seo_description = getattr(erpnext_item, 'ai_seo_description', None) or getattr(erpnext_item, 'seo_meta_description', None)
    seo_keywords = getattr(erpnext_item, 'seo_keywords', None)

    if seo_title:
        payload["metaTitle"] = seo_title
    if seo_description:
        payload["metaDescription"] = seo_description
    if seo_keywords:
        payload["keywords"] = seo_keywords

    # Barcode/EAN
    if erpnext_item.barcodes:
        for barcode in erpnext_item.barcodes:
            if barcode.barcode:
                payload["ean"] = barcode.barcode
                break

    return payload


def get_actual_variant_values(template_item_code: str, attribute_name: str) -> List[str]:
    """
    Get unique attribute values actually used by enabled variants of a template.

    Args:
        template_item_code: Template item code
        attribute_name: Attribute name

    Returns:
        List of attribute values
    """
    variants = frappe.get_all(
        "Item",
        filters={"variant_of": template_item_code, "disabled": 0},
        pluck="name"
    )

    if not variants:
        return []

    values = frappe.get_all(
        "Item Variant Attribute",
        filters={"parent": ["in", variants], "attribute": attribute_name},
        pluck="attribute_value",
        distinct=1
    )

    return [v for v in values if v]
