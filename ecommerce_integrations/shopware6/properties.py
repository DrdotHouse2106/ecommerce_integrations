"""
Shopware 6 Product Properties Export Module

REFACTORED: Now uses export/property_handler.py as the central property management module.
This file provides backwards-compatible hooks and utility functions for jattr_* fields.

For new code, prefer using:
    from ecommerce_integrations.shopware6.export import (
        get_or_create_property_group,
        get_or_create_property_option,
        get_item_properties,
    )
"""

from typing import Any, Dict, List, Optional

import frappe
from frappe.utils import cstr

from ecommerce_integrations.shopware6.connection import (
    get_shopware_client,
    temp_shopware_session,
)
from ecommerce_integrations.shopware6.constants import (
    MODULE_NAME,
    SETTING_DOCTYPE,
)
from ecommerce_integrations.shopware6.export.utils import (
    get_shopware_document_id,
)
from ecommerce_integrations.shopware6.export.property_handler import (
    get_or_create_property_group as _get_or_create_property_group,
    get_or_create_property_option as _get_or_create_property_option,
)


# Prefix for JTL attribute custom fields
JATTR_PREFIX = "jattr_"


def sanitize_field_to_label(fieldname: str) -> str:
    """Convert a fieldname like 'jattr_farbe_gestell' to a readable label."""
    if fieldname.startswith(JATTR_PREFIX):
        fieldname = fieldname[len(JATTR_PREFIX):]
    return fieldname.replace('_', ' ').title()


def get_jattr_fields() -> List[Dict[str, str]]:
    """Get all Custom Fields on Item doctype that start with jattr_."""
    return frappe.get_all(
        "Custom Field",
        filters={"dt": "Item", "fieldname": ["like", f"{JATTR_PREFIX}%"]},
        fields=["fieldname", "label"]
    )


def get_item_jattr_properties(item_code: str) -> Dict[str, str]:
    """
    Get all jattr_* field values for an item.

    Args:
        item_code: ERPNext Item code

    Returns:
        Dict of {label: value} for non-empty jattr_* fields
    """
    jattr_fields = get_jattr_fields()
    if not jattr_fields:
        return {}

    item = frappe.get_doc("Item", item_code)
    properties = {}

    for field in jattr_fields:
        value = getattr(item, field["fieldname"], None)
        if value:
            label = field["label"] or sanitize_field_to_label(field["fieldname"])
            properties[label] = cstr(value).strip()

    return properties


@temp_shopware_session
def sync_item_properties_to_shopware(client, item_code: str) -> Dict[str, Any]:
    """
    Sync an ERPNext Item's properties (jattr_* fields) to Shopware.

    Uses the central property_handler functions for consistency.

    Args:
        client: Shopware API client (injected by decorator)
        item_code: ERPNext Item code

    Returns:
        Dict with sync results
    """
    shopware_product_id = get_shopware_document_id("Item", item_code)

    if not shopware_product_id:
        return {
            "success": False,
            "message": f"Item {item_code} is not synced with Shopware"
        }

    # Get jattr_* properties
    properties = get_item_jattr_properties(item_code)

    if not properties:
        return {
            "success": True,
            "message": f"No jattr_* properties to sync for {item_code}",
            "synced": 0
        }

    # Build list of property option IDs using central functions
    property_option_ids = []

    for group_name, option_value in properties.items():
        group_id = _get_or_create_property_group(client, group_name)
        if not group_id:
            continue

        option_id = _get_or_create_property_option(client, group_id, group_name, option_value)
        if option_id:
            property_option_ids.append({"id": option_id})

    if not property_option_ids:
        return {
            "success": False,
            "message": f"Failed to create property options for {item_code}"
        }

    try:
        client.request_patch(
            f"product/{shopware_product_id}",
            {"properties": property_option_ids}
        )

        return {
            "success": True,
            "message": f"Synced {len(property_option_ids)} jattr properties for {item_code}",
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
    Sync jattr_* properties for all synced items to Shopware.

    Args:
        limit: Maximum number of items to sync

    Returns:
        Dict with sync results
    """
    setting = frappe.get_doc(SETTING_DOCTYPE)

    if not setting.is_enabled():
        return {"success": False, "message": "Shopware integration is not enabled"}

    ecommerce_items = frappe.get_all(
        "Ecommerce Item",
        filters={"integration": MODULE_NAME, "has_variants": 0},
        fields=["erpnext_item_code"],
        limit=int(limit)
    )

    synced = 0
    errors = 0

    for ecom_item in ecommerce_items:
        try:
            result = sync_item_properties_to_shopware(item_code=ecom_item.erpnext_item_code)
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
    Hook: Sync item properties after Item save.

    Called by hooks.py on Item after_save.
    Queues the property sync to avoid blocking the save.
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
    """Manually sync jattr_* properties for a single item."""
    return sync_item_properties_to_shopware(item_code=item_code)


@frappe.whitelist()
def create_all_property_groups() -> Dict[str, Any]:
    """Pre-create all PropertyGroups in Shopware based on jattr_* Custom Fields."""
    jattr_fields = get_jattr_fields()

    if not jattr_fields:
        return {"success": False, "message": "No jattr_* custom fields found"}

    client = get_shopware_client()
    created = 0
    errors = 0

    for field in jattr_fields:
        label = field["label"] or sanitize_field_to_label(field["fieldname"])
        try:
            group_id = _get_or_create_property_group(client, label)
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
