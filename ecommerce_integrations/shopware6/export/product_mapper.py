"""
Shopware 6 Product Mapper

Maps ERPNext Item fields to Shopware product payload.
Handles price calculation, tax rates, and SEO fields.
"""

from typing import Any

import frappe
from frappe.utils import flt

# Note: Internal functions - client is passed from caller
from ecommerce_integrations.shopware6.base.cache_manager import get_cache
from ecommerce_integrations.shopware6.constants import ITEM_SELLING_RATE_FIELD
from ecommerce_integrations.shopware6.export.utils import generate_uuid
from ecommerce_integrations.shopware6.utils import get_logger


def get_tax_id_by_rate(client, tax_rate: float = 19.0) -> str | None:
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
        get_logger().error(f"Failed to get tax ID for rate {tax_rate}%: {e}", persist=True)
        return None


def _parse_delivery_time(delivery_time_name: str) -> tuple:
    """
    Parse delivery time name into (min, max, unit) values.

    Examples:
        "25 Tage" -> (25, 25, 'day')
        "3-5 Werktage" -> (3, 5, 'day')
        "2 Wochen" -> (2, 2, 'week')
        "sofort lieferbar" -> (1, 3, 'day')
    """
    import re

    name_lower = delivery_time_name.lower()

    if "sofort" in name_lower or "lagernd" in name_lower:
        return 1, 3, 'day'

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

    return min_val, max_val, unit


def get_or_create_delivery_time(client, delivery_time_name: str) -> str | None:
    """
    Get existing or create new Delivery Time in Shopware.
    Updates min/max/unit on existing entities if they don't match the parsed name.

    Args:
        client: Shopware API client
        delivery_time_name: Name of the delivery time

    Returns:
        Shopware delivery time ID
    """
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
            existing = times[0].get("attributes", times[0])
            existing_min = existing.get("min", 0)
            existing_max = existing.get("max", 0)
            existing_unit = existing.get("unit", "day")

            # Ensure min/max/unit match the parsed name
            expected_min, expected_max, expected_unit = _parse_delivery_time(delivery_time_name)
            if existing_min != expected_min or existing_max != expected_max or existing_unit != expected_unit:
                get_logger().info(
                    f"Updating DeliveryTime '{delivery_time_name}': "
                    f"min {existing_min}->{expected_min}, max {existing_max}->{expected_max}, "
                    f"unit {existing_unit}->{expected_unit}"
                )
                client.request_patch(f"delivery-time/{dt_id}", {
                    "min": expected_min,
                    "max": expected_max,
                    "unit": expected_unit,
                })

            cache.set("delivery_time", delivery_time_name, dt_id)
            return dt_id

        # Parse and create new delivery time
        min_val, max_val, unit = _parse_delivery_time(delivery_time_name)

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
        get_logger().error(f"Failed to get/create DeliveryTime {delivery_time_name}: {e}", persist=True)
        return None


def get_or_create_manufacturer(client, manufacturer_name: str) -> str | None:
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
        get_logger().error(f"Failed to get/create Manufacturer {manufacturer_name}: {e}", persist=True)
        return None


def get_cached_currency_id(client, currency_code: str = "EUR") -> str | None:
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
        get_logger().error(f"Failed to get currency ID for {currency_code}: {e}", persist=True)

    return None


def get_cached_sales_channel_id(client) -> str | None:
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
        get_logger().error(f"Failed to get sales channel ID: {e}", persist=True)

    return None


def map_erpnext_item_to_shopware(erpnext_item) -> dict[str, Any]:
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

    is_template = getattr(erpnext_item, 'has_variants', 0) == 1

    # Determine active status
    # If item is disabled in ERPNext, it's inactive in Shopware
    # If item is a template (parent) and export_parent_products_inactive is enabled, it's inactive
    active = not erpnext_item.disabled
    if is_template and active:
        settings = frappe.get_cached_doc("Shopware Setting")
        if getattr(settings, 'export_parent_products_inactive', True):
            active = False

    payload = {
        "productNumber": erpnext_item.item_code,
        "name": erpnext_item.item_name,
        "description": description,
        "active": active,
        "stock": 0,
        "isCloseout": False,
        "markAsTopseller": bool(getattr(erpnext_item, 'shopware_topseller', 0)),
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
            # If no variant price found, leave price at 0 - will be caught by validation
        except Exception:
            pass  # Leave price at 0 - will be caught by validation

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

    # CRITICAL: Do not use 0.01 fallback - products without valid prices should be skipped
    # EXCEPTION: Template items (has_variants=1) don't need prices - their variants have the prices
    # The template_handler will set price to 0 for templates after this function returns
    is_template = getattr(erpnext_item, 'has_variants', 0) == 1
    if price <= 0 and not is_template:
        # Log warning and raise exception to prevent invalid price sync
        get_logger().error("Error occurred", persist=False)
        raise ValueError(f"No valid price for item {erpnext_item.name}. Cannot sync to Shopware with price <= 0.")

    net_price = price
    gross_price = round(net_price * (1 + tax_rate / 100), 2)

    # Check for listPrice (Streichpreis/UVP)
    from ecommerce_integrations.shopware6.export.price_handler import _get_list_price_payload
    list_price = _get_list_price_payload(erpnext_item.name, net_price, tax_rate, None)

    price_entry = {
        "currencyId": None,
        "gross": gross_price,
        "net": net_price,
        "linked": False,
    }
    if list_price:
        # currencyId will be set later by the caller when it's available
        list_price["currencyId"] = None
        price_entry["listPrice"] = list_price

    payload["price"] = [price_entry]
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

    # Manufacturer Part Number (HAN/MPN)
    manufacturer_part_no = getattr(erpnext_item, 'default_manufacturer_part_no', None)
    if manufacturer_part_no:
        payload["manufacturerNumber"] = manufacturer_part_no

    # Barcode/EAN
    if erpnext_item.barcodes:
        for barcode in erpnext_item.barcodes:
            if barcode.barcode:
                payload["ean"] = barcode.barcode
                break

    return payload


def get_actual_variant_values(template_item_code: str, attribute_name: str) -> list[str]:
    """
    Get unique attribute values actually used by enabled variants of a template.

    Args:
        template_item_code: Template item code
        attribute_name: Attribute name

    Returns:
        List of attribute values
    """
    # Include disabled variants - their attribute values are still needed
    variants = frappe.get_all(
        "Item",
        filters={"variant_of": template_item_code},
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


def get_product_visibilities(item, setting) -> list[dict[str, Any]] | None:
    """
    Calculate which Sales Channels a product should be visible in.

    Priority:
    1. If item.shopware_all_channels: return ALL channels with visibility 30
    2. If item.shopware_use_item_group_channels: check Item Group mappings
    3. Else: use item.shopware_channel_overrides table
    4. Fallback: use default channel

    Args:
        item: ERPNext Item document
        setting: Shopware Setting document

    Returns:
        List of visibility dicts: [{"salesChannelId": "...", "visibility": 30}, ...]
        or None if no multi-channel config
    """
    # Get all available channels from setting
    all_channels = {sc.sales_channel_id: sc for sc in (setting.sales_channels or [])}

    if not all_channels:
        # No multi-channel config - return None to use legacy single-channel mode
        return None

    visibilities = []

    # Priority 1: Visible in ALL channels
    if getattr(item, 'shopware_all_channels', False):
        return [
            {"salesChannelId": sc_id, "visibility": 30}
            for sc_id, sc in all_channels.items() if sc.active
        ]

    # Priority 2: Catalog Mirror + Smart Collections (unified resolver).
    # The resolver combines per-item overrides, Catalog Mirror placements
    # and Smart Collections with Variant A precedence (highest visibility
    # wins per channel). See ecommerce_integrations/catalog_mirror/resolver.py.
    if getattr(item, 'shopware_use_item_group_channels', True):
        from ecommerce_integrations.catalog_mirror.constants import BACKEND_SHOPWARE
        from ecommerce_integrations.catalog_mirror.resolver import (
            channels_for_item,
        )
        entries = channels_for_item(item.name, BACKEND_SHOPWARE)
        if entries:
            return [
                {
                    "salesChannelId": e.sales_channel,
                    "visibility": e.visibility,
                }
                for e in entries
                if e.sales_channel in all_channels and e.visibility > 0
            ]

    # Priority 3: Per-item overrides
    overrides = getattr(item, 'shopware_channel_overrides', [])
    if overrides:
        for override in overrides:
            if override.visibility != "Exclude":
                vis_value = _parse_visibility(override.visibility)
                visibilities.append({
                    "salesChannelId": override.sales_channel_id,
                    "visibility": vis_value
                })
        if visibilities:
            return visibilities

    # Fallback: Use default channel
    for sc in setting.sales_channels or []:
        if sc.is_default and sc.active:
            return [{"salesChannelId": sc.sales_channel_id, "visibility": 30}]

    # Ultimate fallback: first active channel
    for sc in setting.sales_channels or []:
        if sc.active:
            return [{"salesChannelId": sc.sales_channel_id, "visibility": 30}]

    return None


def _parse_visibility(visibility_str: str) -> int:
    """
    Parse visibility string to Shopware integer value.

    Args:
        visibility_str: Visibility string like "All (30)" or "Search & List (20)"

    Returns:
        Integer visibility value (10, 20, or 30)
    """
    if not visibility_str:
        return 30

    if "30" in visibility_str or "All" in visibility_str:
        return 30
    elif "20" in visibility_str or "Search" in visibility_str:
        return 20
    elif "10" in visibility_str or "Link" in visibility_str:
        return 10

    return 30  # Default


def build_channel_prices(
    item,
    visibilities: list[dict[str, Any]],
    setting,
    client,
    tax_rate: float = 19.0,
    currency_id: str = None
) -> list[dict[str, Any]]:
    """
    Build multi-channel prices array with Shopware Rules.

    Creates a prices array where each entry is linked to a sales channel rule.
    This enables channel-specific pricing in Shopware.

    Args:
        item: ERPNext Item document
        visibilities: List of visibility dicts with salesChannelId
        setting: Shopware Setting document
        client: Shopware API client
        tax_rate: Tax rate for gross price calculation
        currency_id: Shopware currency ID

    Returns:
        List of price dicts with ruleId for each channel
    """
    from ecommerce_integrations.shopware6.export.price_handler import get_channel_price
    from ecommerce_integrations.shopware6.export.rule_handler import get_channel_rule_id

    prices = []
    processed_channels = set()

    # Build lookup for sales channels
    channel_lookup = {
        sc.sales_channel_id: sc
        for sc in (setting.sales_channels or [])
    }

    for vis in visibilities:
        channel_id = vis.get("salesChannelId")
        if not channel_id or channel_id in processed_channels:
            continue

        processed_channels.add(channel_id)

        # Get channel config
        channel = channel_lookup.get(channel_id)
        if not channel:
            continue

        # Get channel-specific price (pass tax_rate for gross-to-net conversion if needed)
        net_price = get_channel_price(item.name, channel, setting, tax_rate=tax_rate)
        if net_price <= 0:
            # Skip this channel - no valid price available
            frappe.logger().warning(
                f"No valid price for {item.name} in channel {channel_id}. Skipping channel price."
            )
            continue

        gross_price = round(net_price * (1 + tax_rate / 100), 2)

        # Check for listPrice (Streichpreis/UVP)
        from ecommerce_integrations.shopware6.export.price_handler import _get_list_price_payload
        list_price = _get_list_price_payload(item.name, net_price, tax_rate, currency_id)

        # Get rule ID for this channel
        rule_id = get_channel_rule_id(client, channel_id, setting)

        # Shopware prices structure requires nested price array
        inner_price = {
            "currencyId": currency_id,
            "gross": gross_price,
            "net": net_price,
            "linked": False,
        }
        if list_price:
            inner_price["listPrice"] = list_price

        price_entry = {
            "quantityStart": 1,
            "price": [inner_price]
        }

        # Only add ruleId if we have one (otherwise it's the default price)
        if rule_id:
            price_entry["ruleId"] = rule_id

        prices.append(price_entry)

    # Ensure at least one price exists (without ruleId = default)
    if not prices:
        from ecommerce_integrations.shopware6.export.price_handler import get_item_price
        default_price_list = getattr(setting, 'default_selling_price_list', None)
        net_price = get_item_price(item.name, default_price_list)
        if net_price <= 0:
            # CRITICAL: No valid price - raise exception instead of using 0.01
            raise ValueError(
                f"No valid price for item {item.name} in any channel or default price list. "
                "Cannot sync to Shopware without a valid price."
            )
        gross_price = round(net_price * (1 + tax_rate / 100), 2)

        # Check for listPrice (Streichpreis/UVP)
        from ecommerce_integrations.shopware6.export.price_handler import _get_list_price_payload
        fallback_list_price = _get_list_price_payload(item.name, net_price, tax_rate, currency_id)

        fallback_inner = {
            "currencyId": currency_id,
            "gross": gross_price,
            "net": net_price,
            "linked": False,
        }
        if fallback_list_price:
            fallback_inner["listPrice"] = fallback_list_price

        prices.append({
            "quantityStart": 1,
            "price": [fallback_inner]
        })

    return prices
