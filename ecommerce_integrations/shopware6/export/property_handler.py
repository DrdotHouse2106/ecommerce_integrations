"""
Shopware 6 Property Handler

Manages property groups, property options, and custom fields for products.
Handles both product properties (filterable attributes) and variant options.
"""

from typing import Any

import frappe
from frappe.utils import cstr

from ecommerce_integrations.ecommerce_integrations.ecommerce_custom_fields import PROP_IS_SURCHARGE
from ecommerce_integrations.shopware6.base.cache_manager import get_cache
from ecommerce_integrations.shopware6.export.utils import generate_uuid


def get_or_create_property_group(client, group_name: str) -> str | None:
    """
    Get existing or create new PropertyGroup in Shopware.

    Args:
        client: Shopware API client
        group_name: Name of the property group

    Returns:
        Property group ID if successful, None otherwise
    """
    cache = get_cache()
    cached_id = cache.get_property_group_id(group_name)
    if cached_id:
        return cached_id

    try:
        # Search for existing
        response = client.request_post(
            "search/property-group",
            {"filter": [{"type": "equals", "field": "name", "value": group_name}]}
        )
        groups = response.data or []

        if groups:
            group_id = groups[0]["id"]
            cache.set_property_group_id(group_name, group_id)
            return group_id

        # Create new
        group_id = generate_uuid(f"property_group_{group_name}")
        client.request_post("property-group", {
            "id": group_id,
            "name": group_name,
            "displayType": "text",
            "sortingType": "alphanumeric",
            "filterable": True,
            "visibleOnProductDetailPage": True,
        })
        cache.set_property_group_id(group_name, group_id)
        return group_id

    except Exception as e:
        get_logger().error(f"Failed to get/create PropertyGroup {group_name}: {e}", persist=False)
        return None


def get_or_create_property_option(client, group_id: str, group_name: str, option_value: str) -> str | None:
    """
    Get existing or create new PropertyGroupOption.

    Args:
        client: Shopware API client
        group_id: Property group ID
        group_name: Property group name (for cache key)
        option_value: Option value

    Returns:
        Property option ID if successful, None otherwise
    """
    if not option_value or not cstr(option_value).strip():
        frappe.logger().warning(f"Skipping empty property option value for group {group_name}")
        return None

    option_value = cstr(option_value).strip()
    cache = get_cache()
    cached_id = cache.get_property_option_id(group_name, option_value)
    if cached_id:
        return cached_id

    try:
        # Search for existing
        response = client.request_post(
            "search/property-group-option",
            {"filter": [
                {"type": "equals", "field": "groupId", "value": group_id},
                {"type": "equals", "field": "name", "value": option_value}
            ]}
        )
        options = response.data or []

        if options:
            option_id = options[0]["id"]
            cache.set_property_option_id(group_name, option_value, option_id)
            return option_id

        # Create new
        option_id = generate_uuid(f"property_option_{group_name}_{option_value}")
        payload = {"id": option_id, "groupId": group_id, "name": option_value}

        try:
            client.request_post("property-group-option", payload)
        except BaseException as e:
            if "WRITE_TYPE_INTEND_ERROR" in str(e):
                client.request_patch(f"property-group-option/{option_id}", payload)
            else:
                raise

        cache.set_property_option_id(group_name, option_value, option_id)
        return option_id

    except Exception as e:
        get_logger().error(f"Failed to get/create PropertyOption {group_name}: {e}", persist=False)
        return None


def get_or_create_variant_option(client, group_id: str, group_name: str, option_value: str) -> str | None:
    """
    Get existing or create new PropertyGroupOption for variant attributes.

    Separate from property options because variant options may need different handling.

    Args:
        client: Shopware API client
        group_id: Property group ID
        group_name: Property group name
        option_value: Option value

    Returns:
        Property option ID if successful, None otherwise
    """
    if not option_value or not cstr(option_value).strip():
        frappe.logger().warning(f"Skipping empty variant option value for group {group_name}")
        return None

    option_value = cstr(option_value).strip()
    cache = get_cache()
    cache_key = f"variant_{group_name}:{option_value}"
    cached_id = cache.get("variant_option", cache_key)
    if cached_id:
        return cached_id

    try:
        # Search for existing
        response = client.request_post(
            "search/property-group-option",
            {"filter": [
                {"type": "equals", "field": "groupId", "value": group_id},
                {"type": "equals", "field": "name", "value": option_value}
            ]}
        )
        options = response.data or []

        if options:
            option_id = options[0]["id"]
            cache.set("variant_option", cache_key, option_id)
            return option_id

        # Create new
        option_id = generate_uuid(f"variant_option_{group_name}_{option_value}")
        payload = {"id": option_id, "groupId": group_id, "name": option_value}

        try:
            client.request_post("property-group-option", payload)
        except BaseException as e:
            if "WRITE_TYPE_INTEND_ERROR" in str(e):
                client.request_patch(f"property-group-option/{option_id}", payload)
            else:
                raise

        cache.set("variant_option", cache_key, option_id)
        return option_id

    except Exception as e:
        get_logger().error(f"Failed to get/create variant option {group_name}: {e}", persist=False)
        return None


def sync_surcharge_properties_batch(limit: int = 500) -> dict[str, Any]:
    """
    Batch sync Surcharge properties for all items where is_sales_item = 0.

    This can be run during reconciliation to ensure all Surcharge items
    have the is_surcharge custom field property set.

    Args:
        limit: Maximum number of items to process

    Returns:
        Dict with statistics
    """
    stats = {
        "checked": 0,
        "added": 0,
        "already_set": 0,
        "errors": 0
    }

    # Surcharge detection: is_sales_item=0 OR item_name starts with "Mehrpreis"
    items = frappe.get_all(
        "Item",
        filters={"has_variants": 0},
        or_filters={"is_sales_item": 0, "item_name": ["like", "Mehrpreis%"]},
        fields=["name"],
        limit=limit,
    )

    for item in items:
        stats["checked"] += 1

        try:
            item_doc = frappe.get_doc("Item", item.name)

            # Check if property already exists
            existing_props = getattr(item_doc, 'ecommerce_properties', []) or []
            has_surcharge = any(
                prop.property_name == PROP_IS_SURCHARGE
                for prop in existing_props
            )

            if has_surcharge:
                stats["already_set"] += 1
                continue

            # Add the property (synced to all ecommerce backends)
            item_doc.append('ecommerce_properties', {
                'property_name': PROP_IS_SURCHARGE,
                'property_type': 'Custom Field',
                'property_value': 'true',
                'sync_to_shopware': 1,
                'sync_to_medusa': 1,
            })
            item_doc.save(ignore_permissions=True)
            stats["added"] += 1

        except Exception:
            stats["errors"] += 1
            get_logger().error("Error occurred", persist=False)

        # Commit periodically
        if stats["checked"] % 50 == 0:
            frappe.db.commit()

    frappe.db.commit()

    frappe.logger("shopware6").info(
        f"Surcharge batch sync: {stats['checked']} checked, "
        f"{stats['added']} added, {stats['already_set']} already set, "
        f"{stats['errors']} errors"
    )

    return stats


def get_logger():
    """Get the Shopware logger."""
    from ecommerce_integrations.shopware6.utils import get_logger as _get_logger
    return _get_logger("PropertyHandler")
