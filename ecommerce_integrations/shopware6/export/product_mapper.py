"""
Shopware 6 Product Mapper

Shared lookup/get-or-create helpers (tax, delivery time, manufacturer,
unit, currency, sales channel) used by the product-sync engine's
Shopware adapter. The full ERPNext-Item -> Shopware-payload mapping
(``map_erpnext_item_to_shopware``) and its variant/visibility/channel-price
helpers lived here too until the legacy template/variant uploader they
served (``export/template_handler.py`` and ``export/variant_handler.py``)
was replaced by the modern engine and removed — see git history if you
need that code back.
"""

# Note: Internal functions - client is passed from caller
from ecommerce_integrations.shopware6.base.cache_manager import get_cache
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
        taxes = response.data or []

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
            taxes = response.data or []
            if taxes:
                tax_id = taxes[0]["id"]
                cache.set("tax", str(tax_rate), tax_id)
                return tax_id

        # Ultimate fallback
        response = client.request_post("search/tax", {"limit": 1})
        taxes = response.data or []
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
        times = response.data or []

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
        manufacturers = response.data or []

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


def get_or_create_unit(client, unit_name: str) -> str | None:
    """
    Get existing or create new (Grundpreis-)Unit in Shopware.

    ``unit_name`` is WeClapp's free-form ``grundpreis_masseinheit`` value
    (e.g. "m²", "L", "Stück") — we have no reliable separate shortCode,
    so a newly-created unit uses the same string for both ``name`` and
    ``shortCode``. Searches by ``name`` first, falls back to
    ``shortCode`` (Shopware ships several default units under short
    codes like "m²"/"l"/"kg" that WeClapp's values are likely to match).

    Args:
        client: Shopware API client
        unit_name: Grundpreis unit label (e.g. "m²")

    Returns:
        Shopware unit ID
    """
    if not unit_name:
        return None

    cache = get_cache()
    cached_id = cache.get("unit", unit_name)
    if cached_id:
        return cached_id

    try:
        response = client.request_post(
            "search/unit",
            {"filter": [{"type": "equals", "field": "name", "value": unit_name}], "limit": 1}
        )
        units = response.data or []

        if not units:
            response = client.request_post(
                "search/unit",
                {"filter": [{"type": "equals", "field": "shortCode", "value": unit_name}], "limit": 1}
            )
            units = response.data or []

        if units:
            unit_id = units[0]["id"]
            cache.set("unit", unit_name, unit_id)
            return unit_id

        unit_id = generate_uuid(f"unit_{unit_name}")
        client.request_post("unit", {"id": unit_id, "name": unit_name, "shortCode": unit_name})
        cache.set("unit", unit_name, unit_id)
        return unit_id

    except Exception as e:
        get_logger().error(f"Failed to get/create Unit {unit_name}: {e}", persist=True)
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
        currencies = response.data or []
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
        sales_channels = response.data or []
        if sales_channels:
            sc_id = sales_channels[0]["id"]
            cache.set_sales_channel_id(sc_id)
            return sc_id
    except Exception as e:
        get_logger().error(f"Failed to get sales channel ID: {e}", persist=True)

    return None
