"""
Shopware 6 Export Utilities

Common utility functions used across export modules.
"""

import hashlib
import re

import frappe

from ecommerce_integrations.shopware6.constants import SETTING_DOCTYPE


def generate_uuid(name: str) -> str:
    """
    Generate a deterministic Shopware-compatible UUID from a string.

    Uses MD5 hash to ensure the same input always produces the same UUID.
    This allows for idempotent operations (create or update).

    Args:
        name: Input string to hash

    Returns:
        32-character hex string (MD5 hash)
    """
    return hashlib.md5(name.encode('utf-8')).hexdigest()


def sanitize_filename(filename: str) -> str:
    """
    Sanitize filename for Shopware media upload.

    Shopware does not allow certain characters in filenames:
    | < > : " / \\ ? *

    Args:
        filename: Original filename

    Returns:
        Sanitized filename with illegal characters replaced by underscore
    """
    illegal_chars = r'[|<>:"/\\?*]'
    return re.sub(illegal_chars, '_', filename)



# Re-export from main utils for backwards compatibility
from ecommerce_integrations.shopware6.utils import get_shopware_document_id


def get_setting():
    """
    Get the Shopware Setting document (cached).

    Returns:
        ShopwareSetting document
    """
    return frappe.get_cached_doc(SETTING_DOCTYPE)


def get_field_mappings() -> dict[str, dict]:
    """
    Get all configured field mappings from Shopware Settings.

    Reads from the product_field_mappings table in Shopware Settings,
    plus auto-detected jattr_* fields if enabled.

    Returns:
        Dict with structure:
        {
            'erpnext_field': {
                'shopware_field': 'field_name',
                'mapping_type': 'Custom Field|Property|Standard Field',
                'shopware_field_type': 'text|html|...',
                'labels': {'de-DE': 'Label', 'en-GB': 'Label'},
                'filterable': True/False
            }
        }
    """
    setting = get_setting()
    mappings = {}

    # Explicit mappings from table
    for row in setting.get('product_field_mappings', []):
        if row.enabled:
            mappings[row.erpnext_field] = {
                'shopware_field': row.shopware_field,
                'mapping_type': row.mapping_type,
                'shopware_field_type': row.shopware_field_type or 'text',
                'labels': {
                    'de-DE': row.label_de or row.erpnext_field,
                    'en-GB': row.label_en or row.erpnext_field
                },
                'filterable': bool(row.filterable)
            }

    # Auto-detect jattr_* fields if enabled
    jattr_prefix = "jattr_"
    if getattr(setting, 'auto_detect_jattr', True):
        jattr_fields = frappe.get_all(
            "Custom Field",
            filters={"dt": "Item", "fieldname": ["like", f"{jattr_prefix}%"]},
            fields=["fieldname", "label"]
        )
        for field in jattr_fields:
            if field['fieldname'] not in mappings:
                mappings[field['fieldname']] = {
                    'shopware_field': field['fieldname'],
                    'mapping_type': 'Property',
                    'shopware_field_type': 'text',
                    'labels': {
                        'de-DE': field['label'],
                        'en-GB': field['label']
                    },
                    'filterable': True
                }

    return mappings


def get_field_mappings_cached() -> dict[str, dict]:
    """
    Get field mappings with Frappe cache.

    Uses Frappe's built-in caching for performance.
    """
    from ecommerce_integrations.shopware6.base.cache_manager import get_cache

    cache = get_cache()
    mappings = cache.get_field_mappings()

    if mappings is None:
        mappings = get_field_mappings()
        cache.set_field_mappings(mappings)

    return mappings


def invalidate_field_mappings_cache():
    """
    Invalidate the field mappings cache.

    Call this when Shopware Settings are saved.
    """
    from ecommerce_integrations.shopware6.base.cache_manager import get_cache
    get_cache().invalidate_field_mappings()


def get_item_group_hierarchy(item_group_name: str) -> list[str]:
    """
    Get the full hierarchy path of an Item Group from ERPNext.

    Args:
        item_group_name: Name of the Item Group

    Returns:
        List of category names from root to leaf,
        e.g., ["All Item Groups", "Electronics", "Laptops"]
    """
    hierarchy = []
    current = item_group_name

    while current:
        hierarchy.insert(0, current)
        parent = frappe.db.get_value("Item Group", current, "parent_item_group")
        if parent and parent != current:
            current = parent
        else:
            break

    return hierarchy


def get_component_for_field_type(field_type: str) -> str:
    """
    Map Shopware field type to component name.

    Args:
        field_type: Shopware custom field type

    Returns:
        Shopware component name
    """
    component_map = {
        'text': 'sw-field',
        'html': 'sw-text-editor',
        'switch': 'sw-field',
        'number': 'sw-field',
        'select': 'sw-single-select',
        'datetime': 'sw-field',
    }
    return component_map.get(field_type, 'sw-field')
