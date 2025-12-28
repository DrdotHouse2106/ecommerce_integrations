"""
Shopware 6 Product Properties Export Module

Exports ERPNext Item Custom Fields (jattr_*) as Shopware Properties.
Based on the Shopify integration pattern for product export.

Shopware 6 Property Structure:
- PropertyGroup: Container for property options (e.g., "Farbe", "Material")
- PropertyGroupOption: Individual values (e.g., "Blau", "Rot")
- Products reference PropertyGroupOptions via the 'properties' association
"""

from typing import Any, Dict, List, Optional, Set
import hashlib
import re

import frappe
from frappe import _
from frappe.utils import cstr

from ecommerce_integrations.shopware6.connection import (
    get_shopware_client,
    temp_shopware_session,
)
from ecommerce_integrations.shopware6.constants import (
    MODULE_NAME,
    SETTING_DOCTYPE,
)
from ecommerce_integrations.shopware6.utils import (
    create_shopware_log,
    get_shopware_document_id,
)


# Prefix for JTL attribute custom fields
JATTR_PREFIX = "jattr_"

# Cache for property group IDs
_property_group_cache: Dict[str, str] = {}
_property_option_cache: Dict[str, str] = {}


def generate_uuid(name: str) -> str:
    """
    Generate a deterministic Shopware-compatible UUID from a string.

    Shopware uses 32-character hex UUIDs without dashes.
    We use MD5 hash for deterministic generation.
    """
    return hashlib.md5(name.encode('utf-8')).hexdigest()


def sanitize_field_to_label(fieldname: str) -> str:
    """
    Convert a fieldname like 'jattr_farbe_gestell' back to a readable label.

    This is a best-effort conversion - the original label might have been
    different due to character stripping during field creation.
    """
    if fieldname.startswith(JATTR_PREFIX):
        fieldname = fieldname[len(JATTR_PREFIX):]

    # Replace underscores with spaces and title case
    label = fieldname.replace('_', ' ').title()
    return label


def get_jattr_fields() -> List[Dict[str, str]]:
    """
    Get all Custom Fields on Item doctype that start with jattr_.

    Returns:
        List of dicts with fieldname and label
    """
    fields = frappe.get_all(
        "Custom Field",
        filters={
            "dt": "Item",
            "fieldname": ["like", f"{JATTR_PREFIX}%"]
        },
        fields=["fieldname", "label"]
    )
    return fields


def get_item_properties(item_code: str) -> Dict[str, str]:
    """
    Get all jattr_* field values for an item.

    Args:
        item_code: ERPNext Item code

    Returns:
        Dict of {label: value} for non-empty fields
    """
    jattr_fields = get_jattr_fields()

    if not jattr_fields:
        return {}

    fieldnames = [f["fieldname"] for f in jattr_fields]
    field_labels = {f["fieldname"]: f["label"] for f in jattr_fields}

    # Get item values
    item = frappe.get_doc("Item", item_code)

    properties = {}
    for fieldname in fieldnames:
        value = getattr(item, fieldname, None)
        if value:
            label = field_labels.get(fieldname, sanitize_field_to_label(fieldname))
            properties[label] = cstr(value).strip()

    return properties


@temp_shopware_session
def get_or_create_property_group(
    client,
    group_name: str,
    filterable: bool = True
) -> Optional[str]:
    """
    Get existing or create new PropertyGroup in Shopware.

    Args:
        client: Shopware API client
        group_name: Name of the property group
        filterable: Whether this property should be filterable in shop

    Returns:
        PropertyGroup ID (UUID)
    """
    global _property_group_cache

    # Check cache first
    if group_name in _property_group_cache:
        return _property_group_cache[group_name]

    try:
        # Search for existing group
        from lib_shopware6_api_base import Criteria, EqualsFilter

        criteria = Criteria()
        criteria.filter.append(EqualsFilter("name", group_name))

        response = client.request_post("search/property-group", criteria)
        groups = response.get("data", [])

        if groups:
            group_id = groups[0]["id"]
            _property_group_cache[group_name] = group_id
            return group_id

        # Create new property group
        group_id = generate_uuid(f"property_group_{group_name}")

        payload = {
            "id": group_id,
            "name": group_name,
            "displayType": "text",
            "sortingType": "alphanumeric",
            "filterable": filterable,
            "visibleOnProductDetailPage": True,
        }

        client.request_post("property-group", payload)
        _property_group_cache[group_name] = group_id

        frappe.log_error(
            f"Created PropertyGroup: {group_name} ({group_id})",
            "Shopware Property Sync"
        )

        return group_id

    except Exception as e:
        frappe.log_error(
            f"Failed to get/create PropertyGroup {group_name}: {e}",
            "Shopware Property Sync Error"
        )
        return None


@temp_shopware_session
def get_or_create_property_option(
    client,
    group_id: str,
    group_name: str,
    option_value: str
) -> Optional[str]:
    """
    Get existing or create new PropertyGroupOption in Shopware.

    Args:
        client: Shopware API client
        group_id: Parent PropertyGroup ID
        group_name: Name of the property group (for caching)
        option_value: The option value (e.g., "Blau", "Stahl")

    Returns:
        PropertyGroupOption ID (UUID)
    """
    global _property_option_cache

    cache_key = f"{group_name}:{option_value}"

    # Check cache first
    if cache_key in _property_option_cache:
        return _property_option_cache[cache_key]

    try:
        # Search for existing option
        from lib_shopware6_api_base import Criteria, EqualsFilter, MultiFilter

        criteria = Criteria()
        criteria.filter.append(EqualsFilter("groupId", group_id))
        criteria.filter.append(EqualsFilter("name", option_value))

        response = client.request_post("search/property-group-option", criteria)
        options = response.get("data", [])

        if options:
            option_id = options[0]["id"]
            _property_option_cache[cache_key] = option_id
            return option_id

        # Create new option
        option_id = generate_uuid(f"property_option_{group_name}_{option_value}")

        payload = {
            "id": option_id,
            "groupId": group_id,
            "name": option_value,
        }

        client.request_post("property-group-option", payload)
        _property_option_cache[cache_key] = option_id

        return option_id

    except Exception as e:
        frappe.log_error(
            f"Failed to get/create PropertyOption {group_name}:{option_value}: {e}",
            "Shopware Property Sync Error"
        )
        return None


@temp_shopware_session
def sync_item_properties_to_shopware(client, item_code: str) -> Dict[str, Any]:
    """
    Sync an ERPNext Item's properties to Shopware.

    This function:
    1. Gets the Shopware product ID for the item
    2. Reads all jattr_* custom field values
    3. Creates/updates PropertyGroups and Options as needed
    4. Assigns the properties to the product

    Args:
        client: Shopware API client
        item_code: ERPNext Item code

    Returns:
        Dict with sync results
    """
    # Get Shopware product ID
    shopware_product_id = get_shopware_document_id("Item", item_code)

    if not shopware_product_id:
        return {
            "success": False,
            "message": f"Item {item_code} is not synced with Shopware"
        }

    # Get item properties
    properties = get_item_properties(item_code)

    if not properties:
        return {
            "success": True,
            "message": f"No properties to sync for {item_code}",
            "synced": 0
        }

    # Build list of property option IDs
    property_option_ids = []

    for group_name, option_value in properties.items():
        # Get or create property group
        group_id = get_or_create_property_group(
            client, group_name, filterable=True
        )

        if not group_id:
            continue

        # Get or create property option
        option_id = get_or_create_property_option(
            client, group_id, group_name, option_value
        )

        if option_id:
            property_option_ids.append({"id": option_id})

    if not property_option_ids:
        return {
            "success": False,
            "message": f"Failed to create property options for {item_code}"
        }

    # Update product with properties
    try:
        client.request_patch(
            f"product/{shopware_product_id}",
            {"properties": property_option_ids}
        )

        return {
            "success": True,
            "message": f"Synced {len(property_option_ids)} properties for {item_code}",
            "synced": len(property_option_ids)
        }

    except Exception as e:
        return {
            "success": False,
            "message": f"Failed to update product properties: {e}"
        }


@frappe.whitelist()
def sync_all_item_properties(limit: int = 100) -> Dict[str, Any]:
    """
    Sync properties for all synced items to Shopware.

    Args:
        limit: Maximum number of items to sync

    Returns:
        Dict with sync results
    """
    setting = frappe.get_doc(SETTING_DOCTYPE)

    if not setting.is_enabled():
        return {"success": False, "message": "Shopware integration is not enabled"}

    # Get all synced items
    ecommerce_items = frappe.get_all(
        "Ecommerce Item",
        filters={"integration": MODULE_NAME, "has_variants": 0},
        fields=["erpnext_item_code", "integration_item_code"],
        limit=int(limit)
    )

    synced = 0
    errors = 0

    for ecom_item in ecommerce_items:
        try:
            result = sync_item_properties_to_shopware(
                item_code=ecom_item.erpnext_item_code
            )

            if result.get("success"):
                synced += result.get("synced", 0)
            else:
                errors += 1

        except Exception as e:
            errors += 1
            frappe.log_error(
                f"Failed to sync properties for {ecom_item.erpnext_item_code}: {e}",
                "Shopware Property Sync Error"
            )

    return {
        "success": True,
        "items_processed": len(ecommerce_items),
        "properties_synced": synced,
        "errors": errors
    }


def upload_item_properties(doc, method=None):
    """
    Hook to sync item properties after Item save.

    Add this to hooks.py:
    doc_events = {
        "Item": {
            "after_save": "ecommerce_integrations.shopware6.properties.upload_item_properties"
        }
    }
    """
    if doc.flags.from_integration:
        return

    setting = frappe.get_doc(SETTING_DOCTYPE)

    if not setting.is_enabled():
        return

    # Check if item is synced with Shopware
    shopware_id = get_shopware_document_id("Item", doc.name)
    if not shopware_id:
        return

    # Queue property sync
    frappe.enqueue(
        "ecommerce_integrations.shopware6.properties.sync_item_properties_to_shopware",
        queue="short",
        item_code=doc.name,
    )


@frappe.whitelist()
def sync_single_item_properties(item_code: str) -> Dict[str, Any]:
    """
    Manually sync properties for a single item.

    Args:
        item_code: ERPNext Item code

    Returns:
        Dict with sync results
    """
    return sync_item_properties_to_shopware(item_code=item_code)


@frappe.whitelist()
def create_all_property_groups() -> Dict[str, Any]:
    """
    Pre-create all PropertyGroups in Shopware based on Custom Fields.

    This can be run before syncing items to ensure all property groups exist.

    Returns:
        Dict with creation results
    """
    jattr_fields = get_jattr_fields()

    if not jattr_fields:
        return {"success": False, "message": "No jattr_* custom fields found"}

    created = 0
    errors = 0

    for field in jattr_fields:
        label = field["label"]
        try:
            group_id = get_or_create_property_group(group_name=label, filterable=True)
            if group_id:
                created += 1
            else:
                errors += 1
        except Exception as e:
            errors += 1
            frappe.log_error(f"Failed to create PropertyGroup {label}: {e}")

    return {
        "success": True,
        "created": created,
        "errors": errors,
        "total_fields": len(jattr_fields)
    }
