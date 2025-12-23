"""
Shopware 6 Product Export Module

Exports ERPNext Items to Shopware 6, similar to the Shopify integration.
Handles:
- Product creation/update
- SEO fields (metaTitle, metaDescription, keywords)
- Custom properties (jattr_* fields)
- Dimensions and weight
- Prices
- Images (with media upload to Shopware)
- Categories (including multi-category support via Webshop's Website Item Groups)
"""

from typing import Any, Dict, List, Optional, Tuple
import hashlib
import os
import mimetypes
import requests

import frappe
from frappe import _, msgprint
from frappe.utils import cint, cstr, flt, get_files_path

from lib_shopware6_api_base import HEADER_index_asynchronously

from ecommerce_integrations.shopware6.connection import (
    get_shopware_client,
    temp_shopware_session,
)
from ecommerce_integrations.shopware6.constants import (
    MODULE_NAME,
    SETTING_DOCTYPE,
    WEIGHT_TO_ERPNEXT_UOM_MAP,
    ITEM_SELLING_RATE_FIELD,
    SHOPWARE_CUSTOM_FIELD_SET_NAME,
    SHOPWARE_CUSTOM_FIELD_ZUBEHOER,
    SHOPWARE_CUSTOM_FIELD_SICHERHEITSBLAETTER,
    SHOPWARE_CUSTOM_FIELD_PRODUKTBLAETTER,
    PRODUCT_CUSTOM_FIELDS_MAP,
    SHOPWARE_CATEGORY_CUSTOM_FIELD_SET_NAME,
    CATEGORY_FAQ_FIELDS_MAP,
)
from ecommerce_integrations.shopware6.utils import (
    create_shopware_log,
    get_shopware_document_id,
)


# Prefix for JTL attribute custom fields
JATTR_PREFIX = "jattr_"

# Cache for property groups and options (in-memory, per-request)
_property_group_cache: Dict[str, str] = {}
_property_option_cache: Dict[str, str] = {}
_category_cache: Dict[str, str] = {}
_media_folder_cache: Dict[str, str] = {}
_custom_field_set_cache: Dict[str, str] = {}
_category_custom_field_set_cache: Dict[str, str] = {}  # Cache for category custom field set
_variant_option_cache: Dict[str, str] = {}  # Cache for variant option IDs

# Redis cache keys for expensive Shopware API lookups (persist across requests)
REDIS_CACHE_PREFIX = "shopware6:cache:"
REDIS_CURRENCY_KEY = f"{REDIS_CACHE_PREFIX}currency_id"
REDIS_SALES_CHANNEL_KEY = f"{REDIS_CACHE_PREFIX}sales_channel_id"
REDIS_IMAGE_HASH_PREFIX = f"{REDIS_CACHE_PREFIX}image_hash:"
REDIS_FIELD_MAPPINGS_KEY = f"{REDIS_CACHE_PREFIX}field_mappings"
REDIS_CACHE_TTL = 86400  # 24 hours
REDIS_MAPPINGS_TTL = 3600  # 1 hour for field mappings


def get_field_mappings() -> Dict[str, Dict]:
    """
    Get all configured field mappings from Shopware Settings.

    Reads from the product_field_mappings table in Shopware Settings,
    plus auto-detected jattr_* fields if enabled.

    Returns dict with structure:
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
    setting = frappe.get_cached_doc(SETTING_DOCTYPE)
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
    if getattr(setting, 'auto_detect_jattr', True):
        jattr_fields = frappe.get_all(
            "Custom Field",
            filters={"dt": "Item", "fieldname": ["like", f"{JATTR_PREFIX}%"]},
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


def get_field_mappings_cached() -> Dict[str, Dict]:
    """
    Get field mappings with Redis caching.

    Cache is invalidated when Shopware Settings are saved.
    """
    cache = frappe.cache()
    cached = cache.get_value(REDIS_FIELD_MAPPINGS_KEY)
    if cached:
        return cached

    mappings = get_field_mappings()
    cache.set_value(REDIS_FIELD_MAPPINGS_KEY, mappings, expires_in_sec=REDIS_MAPPINGS_TTL)
    return mappings


def invalidate_field_mappings_cache():
    """
    Invalidate the field mappings cache.

    Call this when Shopware Settings are saved.
    """
    cache = frappe.cache()
    cache.delete_value(REDIS_FIELD_MAPPINGS_KEY)


def get_cached_currency_id(client, currency_code: str = "EUR") -> Optional[str]:
    """
    Get currency ID from Redis cache or fetch from Shopware API.

    Currency IDs rarely change, so we cache them for 24 hours.
    This eliminates ~1 API call per product sync.

    Args:
        client: Shopware API client
        currency_code: ISO currency code (default: EUR)

    Returns:
        Currency ID or None
    """
    cache = frappe.cache()
    cache_key = f"{REDIS_CURRENCY_KEY}:{currency_code}"

    # Try to get from cache first
    cached_id = cache.get_value(cache_key)
    if cached_id:
        return cached_id

    # Fetch from Shopware API
    try:
        response = client.request_post(
            "search/currency",
            {"filter": [{"type": "equals", "field": "isoCode", "value": currency_code}], "limit": 1}
        )
        currencies = response.get("data", [])
        if currencies:
            currency_id = currencies[0]["id"]
            # Cache for 24 hours
            cache.set_value(cache_key, currency_id, expires_in_sec=REDIS_CACHE_TTL)
            frappe.logger().debug(f"Cached Shopware currency ID for {currency_code}: {currency_id}")
            return currency_id
    except Exception as e:
        frappe.log_error(f"Failed to get currency ID for {currency_code}: {e}")

    return None


def get_cached_sales_channel_id(client) -> Optional[str]:
    """
    Get default sales channel ID from Redis cache or fetch from Shopware API.

    Sales channel ID rarely changes, so we cache it for 24 hours.
    This eliminates ~1 API call per product sync.

    Args:
        client: Shopware API client

    Returns:
        Sales channel ID or None
    """
    cache = frappe.cache()

    # Try to get from cache first
    cached_id = cache.get_value(REDIS_SALES_CHANNEL_KEY)
    if cached_id:
        return cached_id

    # Fetch from Shopware API
    try:
        response = client.request_get("sales-channel")
        sales_channels = response.get("data", [])
        if sales_channels:
            # Use the first sales channel as default
            sc_id = sales_channels[0]["id"]
            # Cache for 24 hours
            cache.set_value(REDIS_SALES_CHANNEL_KEY, sc_id, expires_in_sec=REDIS_CACHE_TTL)
            frappe.logger().debug(f"Cached Shopware sales channel ID: {sc_id}")
            return sc_id
    except Exception as e:
        frappe.log_error(f"Failed to get sales channel ID: {e}")

    return None


def get_cached_image_hash(item_code: str, position: int) -> Optional[str]:
    """
    Get the stored hash of an image that was previously synced.

    Used for delta-sync: only upload images if content has changed.

    Args:
        item_code: ERPNext item code
        position: Image position (0 = cover image)

    Returns:
        MD5 hash string or None if not cached
    """
    cache = frappe.cache()
    cache_key = f"{REDIS_IMAGE_HASH_PREFIX}{item_code}:{position}"
    return cache.get_value(cache_key)


def set_cached_image_hash(item_code: str, position: int, file_hash: str):
    """
    Store the hash of a synced image.

    Args:
        item_code: ERPNext item code
        position: Image position
        file_hash: MD5 hash of the image file
    """
    cache = frappe.cache()
    cache_key = f"{REDIS_IMAGE_HASH_PREFIX}{item_code}:{position}"
    # Cache image hashes for 7 days (images don't change often)
    cache.set_value(cache_key, file_hash, expires_in_sec=604800)


@frappe.whitelist()
def clear_shopware_cache():
    """
    Clear all Shopware-related caches.

    Call this when Shopware configuration changes or for troubleshooting.
    Can be called from the frontend or via API.

    Returns:
        dict: Status message
    """
    cache = frappe.cache()

    # Clear Redis caches for currency and sales channel
    cache.delete_value(REDIS_SALES_CHANNEL_KEY)

    # Clear currency keys (all common currency codes)
    for code in ["EUR", "USD", "GBP", "CHF"]:
        cache.delete_value(f"{REDIS_CURRENCY_KEY}:{code}")

    # Clear in-memory caches (only those defined at module level)
    global _property_group_cache, _property_option_cache, _category_cache
    global _media_folder_cache, _custom_field_set_cache, _variant_option_cache
    global _category_custom_field_set_cache

    _property_group_cache.clear()
    _property_option_cache.clear()
    _category_cache.clear()
    _media_folder_cache.clear()
    _custom_field_set_cache.clear()
    _variant_option_cache.clear()
    _category_custom_field_set_cache.clear()

    frappe.logger().info("Shopware caches cleared")
    return {"status": "success", "message": "All Shopware caches cleared"}


def generate_uuid(name: str) -> str:
    """Generate a deterministic Shopware-compatible UUID from a string."""
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
    import re
    # Replace illegal characters with underscore
    # Shopware error: "Filename must not contain |"
    illegal_chars = r'[|<>:"/\\?*]'
    return re.sub(illegal_chars, '_', filename)


def _get_component_for_field_type(field_type: str) -> str:
    """Map Shopware field type to component name."""
    component_map = {
        'text': 'sw-field',
        'html': 'sw-text-editor',
        'switch': 'sw-field',
        'number': 'sw-field',
        'select': 'sw-single-select',
        'datetime': 'sw-field',
    }
    return component_map.get(field_type, 'sw-field')


def _build_custom_fields_from_mappings() -> List[Dict[str, Any]]:
    """
    Build Shopware custom fields array from configured mappings.

    Returns list of custom field definitions for the custom field set.
    """
    mappings = get_field_mappings_cached()
    custom_fields = []
    position = 1

    for erpnext_field, config in mappings.items():
        if config['mapping_type'] == 'Custom Field':
            custom_fields.append({
                "id": generate_uuid(f"custom_field_{config['shopware_field']}"),
                "name": config['shopware_field'],
                "type": config['shopware_field_type'],
                "config": {
                    "label": config['labels'],
                    "customFieldPosition": position,
                    "componentName": _get_component_for_field_type(config['shopware_field_type'])
                }
            })
            position += 1

    return custom_fields


@temp_shopware_session
def ensure_shopware_custom_field_set(client) -> Optional[str]:
    """
    Ensure the ERPNext custom field set exists in Shopware.

    Dynamically creates custom fields based on configured field mappings.
    Falls back to legacy hardcoded fields if no mappings are configured.

    Returns:
        Custom field set ID if successful, None otherwise
    """
    global _custom_field_set_cache

    if SHOPWARE_CUSTOM_FIELD_SET_NAME in _custom_field_set_cache:
        return _custom_field_set_cache[SHOPWARE_CUSTOM_FIELD_SET_NAME]

    try:
        # Search for existing custom field set
        response = client.request_post(
            "search/custom-field-set",
            {"filter": [{"type": "equals", "field": "name", "value": SHOPWARE_CUSTOM_FIELD_SET_NAME}]}
        )
        sets = response.get("data", [])

        if sets:
            set_id = sets[0]["id"]
            _custom_field_set_cache[SHOPWARE_CUSTOM_FIELD_SET_NAME] = set_id
            return set_id

        # Build custom fields from mappings
        custom_fields = _build_custom_fields_from_mappings()

        # Fallback to legacy hardcoded fields if no mappings configured
        if not custom_fields:
            custom_fields = [
                {
                    "id": generate_uuid(f"custom_field_{SHOPWARE_CUSTOM_FIELD_ZUBEHOER}"),
                    "name": SHOPWARE_CUSTOM_FIELD_ZUBEHOER,
                    "type": "html",
                    "config": {
                        "label": {
                            "en-GB": "Accessories",
                            "de-DE": "Zubehör"
                        },
                        "customFieldPosition": 1,
                        "componentName": "sw-text-editor"
                    }
                },
                {
                    "id": generate_uuid(f"custom_field_{SHOPWARE_CUSTOM_FIELD_SICHERHEITSBLAETTER}"),
                    "name": SHOPWARE_CUSTOM_FIELD_SICHERHEITSBLAETTER,
                    "type": "html",
                    "config": {
                        "label": {
                            "en-GB": "Safety Data Sheets",
                            "de-DE": "Sicherheitsblätter"
                        },
                        "customFieldPosition": 2,
                        "componentName": "sw-text-editor"
                    }
                },
                {
                    "id": generate_uuid(f"custom_field_{SHOPWARE_CUSTOM_FIELD_PRODUKTBLAETTER}"),
                    "name": SHOPWARE_CUSTOM_FIELD_PRODUKTBLAETTER,
                    "type": "html",
                    "config": {
                        "label": {
                            "en-GB": "Product Data Sheets",
                            "de-DE": "Produktblätter"
                        },
                        "customFieldPosition": 3,
                        "componentName": "sw-text-editor"
                    }
                }
            ]

        # Create new custom field set with fields
        set_id = generate_uuid(f"custom_field_set_{SHOPWARE_CUSTOM_FIELD_SET_NAME}")

        payload = {
            "id": set_id,
            "name": SHOPWARE_CUSTOM_FIELD_SET_NAME,
            "config": {
                "label": {
                    "en-GB": "ERPNext Product Fields",
                    "de-DE": "ERPNext Produktfelder"
                }
            },
            "customFields": custom_fields,
            "relations": [
                {
                    "id": generate_uuid(f"custom_field_set_relation_{SHOPWARE_CUSTOM_FIELD_SET_NAME}_product"),
                    "entityName": "product"
                }
            ]
        }

        client.request_post("custom-field-set", payload)
        _custom_field_set_cache[SHOPWARE_CUSTOM_FIELD_SET_NAME] = set_id

        frappe.logger().info(f"Created Shopware custom field set: {SHOPWARE_CUSTOM_FIELD_SET_NAME}")
        return set_id

    except Exception as e:
        frappe.log_error(f"Failed to create/get Shopware custom field set: {e}")
        return None


def get_item_custom_fields(erpnext_item) -> Dict[str, Any]:
    """
    Get custom field values from ERPNext Item for Shopware sync.

    Priority:
    1. Read from shopware_properties table (new flexible key-value table)
    2. Fallback to old individual custom fields (for backwards compatibility)

    Returns a dict with Shopware custom field names as keys.
    Only includes fields that have values.
    """
    custom_fields = {}

    # Priority 1: Read from new shopware_properties table
    properties_table = getattr(erpnext_item, 'shopware_properties', None) or []
    for row in properties_table:
        if row.property_type == 'Custom Field' and row.property_value:
            # Use property_name as Shopware field name (with erpnext_ prefix for consistency)
            shopware_field_name = f"erpnext_{row.property_name.lower().replace(' ', '_')}"
            custom_fields[shopware_field_name] = cstr(row.property_value).strip()

    # Priority 2: Fallback to old configurable mappings (for backwards compatibility)
    if not custom_fields:
        mappings = get_field_mappings_cached()
        for erpnext_field, config in mappings.items():
            if config['mapping_type'] == 'Custom Field':
                value = getattr(erpnext_item, erpnext_field, None)
                if value:
                    custom_fields[config['shopware_field']] = cstr(value).strip()

    # Priority 3: Fallback to legacy hardcoded mappings
    if not custom_fields:
        for erpnext_field, shopware_field in PRODUCT_CUSTOM_FIELDS_MAP.items():
            value = getattr(erpnext_item, erpnext_field, None)
            if value:
                custom_fields[shopware_field] = cstr(value).strip()

    return custom_fields


def get_shopware_weight_unit(erpnext_uom: str) -> str:
    """Convert ERPNext weight UOM to Shopware weight unit."""
    # Reverse lookup
    for sw_unit, erp_unit in WEIGHT_TO_ERPNEXT_UOM_MAP.items():
        if erp_unit == erpnext_uom:
            return sw_unit
    return "kg"  # Default


@temp_shopware_session
def get_or_create_property_group(client, group_name: str) -> Optional[str]:
    """Get existing or create new PropertyGroup in Shopware."""
    global _property_group_cache

    if group_name in _property_group_cache:
        return _property_group_cache[group_name]

    try:
        # Search for existing
        response = client.request_post(
            "search/property-group",
            {"filter": [{"type": "equals", "field": "name", "value": group_name}]}
        )
        groups = response.get("data", [])

        if groups:
            group_id = groups[0]["id"]
            _property_group_cache[group_name] = group_id
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
        _property_group_cache[group_name] = group_id
        return group_id

    except Exception as e:
        frappe.log_error(f"Failed to get/create PropertyGroup {group_name}: {e}")
        return None


@temp_shopware_session
def get_or_create_property_option(client, group_id: str, group_name: str, option_value: str) -> Optional[str]:
    """Get existing or create new PropertyGroupOption."""
    global _property_option_cache

    # Validate option_value is not empty - Shopware requires a non-blank name
    if not option_value or not cstr(option_value).strip():
        frappe.logger().warning(
            f"Skipping empty property option value for group {group_name}"
        )
        return None

    option_value = cstr(option_value).strip()

    cache_key = f"{group_name}:{option_value}"
    if cache_key in _property_option_cache:
        return _property_option_cache[cache_key]

    try:
        # Search for existing
        response = client.request_post(
            "search/property-group-option",
            {"filter": [
                {"type": "equals", "field": "groupId", "value": group_id},
                {"type": "equals", "field": "name", "value": option_value}
            ]}
        )
        options = response.get("data", [])

        if options:
            option_id = options[0]["id"]
            _property_option_cache[cache_key] = option_id
            return option_id

        # Create new - with fallback to PATCH if already exists
        option_id = generate_uuid(f"property_option_{group_name}_{option_value}")
        payload = {
            "id": option_id,
            "groupId": group_id,
            "name": option_value,
        }
        try:
            client.request_post("property-group-option", payload)
        except BaseException as e:
            if "WRITE_TYPE_INTEND_ERROR" in str(e):
                client.request_patch(f"property-group-option/{option_id}", payload)
            else:
                raise
        _property_option_cache[cache_key] = option_id
        return option_id

    except Exception as e:
        frappe.log_error(f"Failed to get/create PropertyOption {group_name}:{option_value}: {e}")
        return None


@temp_shopware_session
def get_or_create_variant_option(client, group_id: str, group_name: str, option_value: str) -> Optional[str]:
    """
    Get existing or create new PropertyGroupOption for variant attributes.

    This is separate from property options because variant options use a different
    cache and may need different handling in Shopware.
    """
    global _variant_option_cache

    # Validate option_value is not empty - Shopware requires a non-blank name
    if not option_value or not cstr(option_value).strip():
        frappe.logger().warning(
            f"Skipping empty variant option value for group {group_name}"
        )
        return None

    option_value = cstr(option_value).strip()

    cache_key = f"variant_{group_name}:{option_value}"
    if cache_key in _variant_option_cache:
        return _variant_option_cache[cache_key]

    try:
        # Search for existing option
        response = client.request_post(
            "search/property-group-option",
            {"filter": [
                {"type": "equals", "field": "groupId", "value": group_id},
                {"type": "equals", "field": "name", "value": option_value}
            ]}
        )
        options = response.get("data", [])

        if options:
            option_id = options[0]["id"]
            _variant_option_cache[cache_key] = option_id
            return option_id

        # Create new option - with fallback to PATCH if already exists
        option_id = generate_uuid(f"variant_option_{group_name}_{option_value}")
        payload = {
            "id": option_id,
            "groupId": group_id,
            "name": option_value,
        }
        try:
            client.request_post("property-group-option", payload)
        except BaseException as e:
            if "WRITE_TYPE_INTEND_ERROR" in str(e):
                client.request_patch(f"property-group-option/{option_id}", payload)
            else:
                raise
        _variant_option_cache[cache_key] = option_id
        return option_id

    except Exception as e:
        frappe.log_error(f"Failed to get/create variant option {group_name}:{option_value}: {e}")
        return None


def get_template_item_attributes(template_item) -> List[Dict[str, Any]]:
    """
    Get all Item Attributes defined for a template item.

    Returns list of attribute info dicts with name and possible values.
    """
    attributes = []

    if not template_item.attributes:
        return attributes

    for attr_row in template_item.attributes:
        attr_name = attr_row.attribute

        # Get attribute values from Item Attribute doctype
        attr_doc = frappe.get_doc("Item Attribute", attr_name)
        values = []
        if not attr_doc.numeric_values:
            values = [v.attribute_value for v in attr_doc.item_attribute_values]

        attributes.append({
            "name": attr_name,
            "values": values,
        })

    return attributes


def get_variant_attribute_values(variant_item) -> Dict[str, str]:
    """
    Get the attribute values for a variant item.

    Returns dict of {attribute_name: attribute_value}
    """
    attr_values = {}

    if not variant_item.attributes:
        return attr_values

    for attr_row in variant_item.attributes:
        attr_values[attr_row.attribute] = attr_row.attribute_value

    return attr_values


def get_item_group_hierarchy(item_group_name: str) -> List[str]:
    """
    Get the full hierarchy path of an Item Group from ERPNext.

    Args:
        item_group_name: Name of the Item Group

    Returns:
        List of category names from root to leaf, e.g., ["All Item Groups", "Electronics", "Laptops"]
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


@temp_shopware_session
def ensure_category_custom_field_set(client) -> Optional[str]:
    """
    Ensure the ERPNext category custom field set exists in Shopware.

    Creates a custom field set with fields for FAQ questions and answers.

    Returns:
        Custom field set ID if successful, None otherwise
    """
    global _category_custom_field_set_cache

    if SHOPWARE_CATEGORY_CUSTOM_FIELD_SET_NAME in _category_custom_field_set_cache:
        return _category_custom_field_set_cache[SHOPWARE_CATEGORY_CUSTOM_FIELD_SET_NAME]

    try:
        # Search for existing custom field set
        response = client.request_post(
            "search/custom-field-set",
            {"filter": [{"type": "equals", "field": "name", "value": SHOPWARE_CATEGORY_CUSTOM_FIELD_SET_NAME}]}
        )
        sets = response.get("data", [])

        if sets:
            set_id = sets[0]["id"]
            _category_custom_field_set_cache[SHOPWARE_CATEGORY_CUSTOM_FIELD_SET_NAME] = set_id
            return set_id

        # Create new custom field set with FAQ fields
        set_id = generate_uuid(f"custom_field_set_{SHOPWARE_CATEGORY_CUSTOM_FIELD_SET_NAME}")

        # Build custom fields for FAQ 1-5
        custom_fields = []
        position = 1
        for i in range(1, 6):
            # Question field
            q_field_name = f"erpnext_faq{i}_question"
            custom_fields.append({
                "id": generate_uuid(f"custom_field_{q_field_name}"),
                "name": q_field_name,
                "type": "text",
                "config": {
                    "label": {"de-DE": f"FAQ {i} - Frage", "en-GB": f"FAQ {i} - Question"},
                    "customFieldPosition": position,
                },
            })
            position += 1

            # Answer field
            a_field_name = f"erpnext_faq{i}_answer"
            custom_fields.append({
                "id": generate_uuid(f"custom_field_{a_field_name}"),
                "name": a_field_name,
                "type": "html",
                "config": {
                    "label": {"de-DE": f"FAQ {i} - Antwort", "en-GB": f"FAQ {i} - Answer"},
                    "customFieldPosition": position,
                    "componentName": "sw-text-editor",
                },
            })
            position += 1

        payload = {
            "id": set_id,
            "name": SHOPWARE_CATEGORY_CUSTOM_FIELD_SET_NAME,
            "config": {
                "label": {"de-DE": "ERPNext Kategorie-Felder", "en-GB": "ERPNext Category Fields"},
                "translated": True,
            },
            "customFields": custom_fields,
            "relations": [
                {
                    "id": generate_uuid(f"relation_{SHOPWARE_CATEGORY_CUSTOM_FIELD_SET_NAME}_category"),
                    "entityName": "category",
                }
            ],
        }

        client.request_post("custom-field-set", payload)
        _category_custom_field_set_cache[SHOPWARE_CATEGORY_CUSTOM_FIELD_SET_NAME] = set_id
        return set_id

    except BaseException as e:
        frappe.log_error(f"Failed to ensure category custom field set: {e}")
        return None


def get_item_group_data(item_group_name: str) -> Optional[Dict[str, Any]]:
    """
    Get Item Group data including description, FAQ, SEO fields, category image, and Shopware active status.

    Args:
        item_group_name: Name of the Item Group

    Returns:
        Dict with Item Group data or None
    """
    try:
        item_group = frappe.get_doc("Item Group", item_group_name)
        # shopware_active: 1 = active (default), 0 = inactive in Shopware only
        # If field doesn't exist, default to True (active)
        shopware_active = getattr(item_group, "shopware_active", None)
        if shopware_active is None:
            shopware_active = True  # Default: categories are active
        else:
            shopware_active = bool(shopware_active)

        return {
            "name": item_group.name,
            "item_group_name": item_group.item_group_name,
            "description": getattr(item_group, "description", None),
            "image": item_group.image,
            "category_image": getattr(item_group, "category_image", None),
            "seo_title": getattr(item_group, "seo_title", None),
            "seo_meta_description": getattr(item_group, "seo_meta_description", None),
            "seo_keywords": getattr(item_group, "seo_keywords", None),
            "shopware_active": shopware_active,
            "faq1_question": getattr(item_group, "faq1_question", None),
            "faq1_answer": getattr(item_group, "faq1_answer", None),
            "faq2_question": getattr(item_group, "faq2_question", None),
            "faq2_answer": getattr(item_group, "faq2_answer", None),
            "faq3_question": getattr(item_group, "faq3_question", None),
            "faq3_answer": getattr(item_group, "faq3_answer", None),
            "faq4_question": getattr(item_group, "faq4_question", None),
            "faq4_answer": getattr(item_group, "faq4_answer", None),
            "faq5_question": getattr(item_group, "faq5_question", None),
            "faq5_answer": getattr(item_group, "faq5_answer", None),
        }
    except BaseException:
        return None


def build_category_custom_fields(item_group_data: Dict[str, Any]) -> Dict[str, str]:
    """
    Build custom fields dict for category from Item Group data.

    Args:
        item_group_data: Dict with Item Group FAQ fields

    Returns:
        Dict mapping Shopware custom field names to values
    """
    custom_fields = {}

    for erpnext_field, shopware_field in CATEGORY_FAQ_FIELDS_MAP.items():
        value = item_group_data.get(erpnext_field)
        if value:
            custom_fields[shopware_field] = value

    return custom_fields


@temp_shopware_session
def upload_category_media(client, category_id: str, image_path: str) -> Optional[str]:
    """
    Upload and assign media to a category in Shopware.

    Args:
        client: Shopware API client
        category_id: Shopware category ID
        image_path: Path to the image file (can be URL or local path)

    Returns:
        Media ID if successful, None otherwise
    """
    try:
        # Get or create media folder for categories
        folder_id = get_or_create_media_folder.__wrapped__(client, "Category Media")
        if not folder_id:
            return None

        # Generate media ID based on category
        media_id = generate_uuid(f"category_media_{category_id}")

        # Get image content
        if image_path.startswith("http"):
            response = requests.get(image_path, timeout=30)
            if response.status_code != 200:
                return None
            image_content = response.content
            content_type = response.headers.get("Content-Type", "image/jpeg")
            filename = image_path.split("/")[-1].split("?")[0]
        else:
            # Local file
            if image_path.startswith("/files/"):
                full_path = get_files_path() + image_path[6:]
            elif image_path.startswith("/private/files/"):
                full_path = get_files_path(is_private=True) + image_path[14:]
            else:
                full_path = image_path

            if not os.path.exists(full_path):
                return None

            with open(full_path, "rb") as f:
                image_content = f.read()
            content_type = mimetypes.guess_type(full_path)[0] or "image/jpeg"
            filename = sanitize_filename(os.path.basename(full_path))

        # Get file extension
        ext = filename.split(".")[-1].lower() if "." in filename else "jpg"
        if ext not in ["jpg", "jpeg", "png", "gif", "webp"]:
            ext = "jpg"

        # Create media entry
        media_payload = {
            "id": media_id,
            "mediaFolderId": folder_id,
        }
        client.request_post("media", media_payload)

        # Upload media content
        upload_url = f"_action/media/{media_id}/upload?extension={ext}&fileName={filename}"
        client.request_post(
            upload_url,
            image_content,
            headers={"Content-Type": content_type},
            raw_body=True
        )

        return media_id

    except BaseException as e:
        frappe.log_error(f"Failed to upload category media: {e}")
        return None


@temp_shopware_session
def get_or_create_category(client, category_name: str, parent_id: str = None, item_group_data: Dict[str, Any] = None) -> Optional[str]:
    """
    Get existing or create/update Category in Shopware.

    Now includes support for:
    - FAQ fields (as custom fields)
    - SEO fields (metaTitle, metaDescription, keywords)
    - Category image

    Args:
        client: Shopware API client
        category_name: Name of the category
        parent_id: Parent category ID (optional)
        item_group_data: ERPNext Item Group data with FAQ, SEO, image fields (optional)

    Returns:
        Shopware category ID if successful, None otherwise
    """
    global _category_cache

    try:
        # Search for existing category
        response = client.request_post(
            "search/category",
            {"filter": [{"type": "equals", "field": "name", "value": category_name}]}
        )
        categories = response.get("data", [])

        existing_cat_id = None
        if categories:
            existing_cat_id = categories[0]["id"]
            _category_cache[category_name] = existing_cat_id

        # Get root category if no parent specified
        if not parent_id and not existing_cat_id:
            root_resp = client.request_post(
                "search/category",
                {"filter": [{"type": "equals", "field": "parentId", "value": None}], "limit": 1}
            )
            root_cats = root_resp.get("data", [])
            if root_cats:
                parent_id = root_cats[0]["id"]

        # Build category payload
        cat_id = existing_cat_id or generate_uuid(f"category_{category_name}")

        # Determine active status from item_group_data.shopware_active (default: True)
        # This allows hiding categories in Shopware without affecting ERPNext
        is_active = True  # Default
        if item_group_data and "shopware_active" in item_group_data:
            is_active = item_group_data["shopware_active"]

        payload = {
            "id": cat_id,
            "name": category_name,
            "active": is_active,
            "displayNestedProducts": True,
        }

        if parent_id and not existing_cat_id:
            payload["parentId"] = parent_id

        # Add description and SEO fields if available
        if item_group_data:
            # Main category description
            if item_group_data.get("description"):
                payload["description"] = item_group_data["description"]

            # SEO fields
            if item_group_data.get("seo_title"):
                payload["metaTitle"] = item_group_data["seo_title"]
            if item_group_data.get("seo_meta_description"):
                payload["metaDescription"] = item_group_data["seo_meta_description"]
            if item_group_data.get("seo_keywords"):
                payload["keywords"] = item_group_data["seo_keywords"]

            # Build and add custom fields for FAQs
            custom_fields = build_category_custom_fields(item_group_data)
            if custom_fields:
                # Ensure custom field set exists
                ensure_category_custom_field_set.__wrapped__(client)
                payload["customFields"] = custom_fields

        # Create or update category
        if existing_cat_id:
            client.request_patch(f"category/{cat_id}", payload)
        else:
            client.request_post("category", payload)

        _category_cache[category_name] = cat_id

        # Handle category image
        if item_group_data:
            image_path = item_group_data.get("category_image") or item_group_data.get("image")
            if image_path:
                media_id = upload_category_media.__wrapped__(client, cat_id, image_path)
                if media_id:
                    # Assign media to category
                    client.request_patch(f"category/{cat_id}", {"mediaId": media_id})

        return cat_id

    except BaseException as e:
        frappe.log_error(f"Failed to get/create Category {category_name}: {e}")
        return None


@temp_shopware_session
def sync_category_hierarchy(client, item_group_name: str) -> Optional[str]:
    """
    Sync the full category hierarchy from ERPNext Item Group to Shopware.

    Creates all parent categories if they don't exist, maintaining the hierarchy.
    Now also syncs FAQ fields, SEO fields, and category images.

    Args:
        client: Shopware API client
        item_group_name: ERPNext Item Group name

    Returns:
        Shopware category ID of the leaf (most specific) category
    """
    hierarchy = get_item_group_hierarchy(item_group_name)

    if not hierarchy:
        return None

    # Skip root categories that shouldn't be synced to Shopware
    # "All Item Groups" is the ERPNext default root
    # "Alle Artikelgruppen" is the German equivalent
    root_categories_to_skip = ["All Item Groups", "Alle Artikelgruppen"]
    while hierarchy and hierarchy[0] in root_categories_to_skip:
        hierarchy = hierarchy[1:]

    if not hierarchy:
        return None

    # Get Shopware root category
    root_resp = client.request_post(
        "search/category",
        {"filter": [{"type": "equals", "field": "parentId", "value": None}], "limit": 1}
    )
    root_cats = root_resp.get("data", [])
    parent_id = root_cats[0]["id"] if root_cats else None

    # Create each level of the hierarchy
    last_category_id = None
    for category_name in hierarchy:
        # Get Item Group data for this category (FAQ, SEO, image)
        item_group_data = get_item_group_data(category_name)

        category_id = get_or_create_category.__wrapped__(
            client,
            category_name,
            parent_id,
            item_group_data
        )
        if category_id:
            parent_id = category_id
            last_category_id = category_id

    return last_category_id


def get_all_item_categories(item_code: str, include_variant_categories: bool = True) -> List[str]:
    """
    Get all categories (Item Groups) for an item, including multi-category assignments.

    This function supports both:
    1. Standard ERPNext: Uses item.item_group (single category)
    2. Webshop App: Additionally uses Website Item's website_item_groups (multiple categories)
    3. For template/parent items: Also collects categories from all variants

    Args:
        item_code: The ERPNext Item code
        include_variant_categories: If True and item has_variants, also collect categories from variants

    Returns:
        List of unique Item Group names for this item
    """
    categories = []

    try:
        # Get the primary item_group from the Item
        item = frappe.get_doc("Item", item_code)
        if item.item_group:
            categories.append(item.item_group)

        # Check if Webshop app is installed and Website Item exists
        if frappe.db.exists("DocType", "Website Item"):
            # Find Website Item linked to this Item
            website_item_name = frappe.db.get_value(
                "Website Item",
                {"item_code": item_code},
                "name"
            )

            if website_item_name:
                # Get additional categories from website_item_groups child table
                website_item_groups = frappe.get_all(
                    "Website Item Group",
                    filters={"parent": website_item_name, "parenttype": "Website Item"},
                    fields=["item_group"]
                )

                for wig in website_item_groups:
                    if wig.item_group and wig.item_group not in categories:
                        categories.append(wig.item_group)

            # For template/parent items: also collect categories from all variants
            # This ensures multi-categories assigned to variants are inherited by the parent in Shopware
            if include_variant_categories and item.has_variants:
                # Get all variant item codes
                variant_codes = frappe.get_all(
                    "Item",
                    filters={"variant_of": item_code},
                    pluck="name"
                )

                for variant_code in variant_codes:
                    # Get Website Item for this variant
                    variant_website_item = frappe.db.get_value(
                        "Website Item",
                        {"item_code": variant_code},
                        "name"
                    )

                    if variant_website_item:
                        # Get categories from variant's website_item_groups
                        variant_groups = frappe.get_all(
                            "Website Item Group",
                            filters={"parent": variant_website_item, "parenttype": "Website Item"},
                            fields=["item_group"]
                        )

                        for vg in variant_groups:
                            if vg.item_group and vg.item_group not in categories:
                                categories.append(vg.item_group)

    except Exception as e:
        frappe.log_error(f"Error getting categories for item {item_code}: {e}")

    return categories


@temp_shopware_session
def sync_all_item_categories(client, item_code: str) -> List[Dict[str, str]]:
    """
    Sync all categories for an item to Shopware and return category ID list.

    This enables multi-category product listings in Shopware by:
    1. Getting all assigned categories from ERPNext (primary + website item groups)
    2. Syncing each category hierarchy to Shopware
    3. Returning the list of Shopware category IDs

    Args:
        client: Shopware API client
        item_code: The ERPNext Item code

    Returns:
        List of dicts with category IDs for Shopware product payload,
        e.g., [{"id": "abc123"}, {"id": "def456"}]
    """
    categories = get_all_item_categories(item_code)
    category_ids = []
    seen_ids = set()

    for item_group_name in categories:
        try:
            category_id = sync_category_hierarchy.__wrapped__(client, item_group_name)
            if category_id and category_id not in seen_ids:
                seen_ids.add(category_id)
                category_ids.append({"id": category_id})
        except Exception as e:
            frappe.log_error(f"Error syncing category {item_group_name} for item {item_code}: {e}")

    return category_ids


def clear_product_categories(client, product_id: str) -> bool:
    """
    Clear all category assignments from a product in Shopware.

    This is necessary before setting new categories because Shopware's PATCH
    endpoint adds categories instead of replacing them.

    Args:
        client: Shopware API client
        product_id: Shopware product UUID

    Returns:
        True if successful or no categories to clear, False on error
    """
    try:
        # Get current categories
        response = client.request_get(f"product/{product_id}?associations[categories][]")
        product_data = response.get("data", {})
        categories = product_data.get("categories", [])

        if not categories:
            return True  # No categories to clear

        # Delete each category assignment
        for cat in categories:
            cat_id = cat.get("id")
            if cat_id:
                try:
                    # Delete the product-category association
                    client.request_delete(f"product/{product_id}/categories/{cat_id}")
                except Exception as e:
                    # Log but continue - some deletions may fail if already removed
                    frappe.logger().debug(f"Could not delete category {cat_id} from product {product_id}: {e}")

        return True
    except Exception as e:
        frappe.log_error(f"Error clearing categories for product {product_id}: {e}")
        return False


# Cache for delivery times
_delivery_time_cache: Dict[str, str] = {}

# Cache for tax IDs by rate
_tax_id_cache: Dict[float, str] = {}


@temp_shopware_session
def get_tax_id_by_rate(client, tax_rate: float = 19.0) -> Optional[str]:
    """
    Get the Shopware tax ID for a given tax rate.

    Searches for a tax rule with a matching rate. Falls back to standard tax (19%)
    if exact match not found.

    Args:
        client: Shopware API client
        tax_rate: The tax rate to find (e.g., 19.0, 7.0, 0.0)

    Returns:
        Shopware tax ID if found, None otherwise
    """
    global _tax_id_cache

    # Round tax rate to avoid floating point issues
    tax_rate = round(tax_rate, 2)

    if tax_rate in _tax_id_cache:
        return _tax_id_cache[tax_rate]

    try:
        # Search for tax with matching rate
        response = client.request_post(
            "search/tax",
            {
                "filter": [{"type": "equals", "field": "taxRate", "value": tax_rate}],
                "limit": 1
            }
        )
        taxes = response.get("data", [])

        if taxes:
            tax_id = taxes[0]["id"]
            _tax_id_cache[tax_rate] = tax_id
            return tax_id

        # If exact rate not found, try to find the closest standard tax
        # German tax rates: 19% (standard), 7% (reduced), 0% (exempt)
        # If requested rate is close to a standard rate, use that
        standard_rates = [19.0, 7.0, 0.0]
        closest_rate = min(standard_rates, key=lambda x: abs(x - tax_rate))

        if closest_rate != tax_rate:
            response = client.request_post(
                "search/tax",
                {
                    "filter": [{"type": "equals", "field": "taxRate", "value": closest_rate}],
                    "limit": 1
                }
            )
            taxes = response.get("data", [])
            if taxes:
                tax_id = taxes[0]["id"]
                _tax_id_cache[tax_rate] = tax_id
                frappe.logger().info(
                    f"Tax rate {tax_rate}% not found in Shopware, using {closest_rate}% instead"
                )
                return tax_id

        # Ultimate fallback: get any tax (should not happen in properly configured shops)
        response = client.request_post("search/tax", {"limit": 1})
        taxes = response.get("data", [])
        if taxes:
            tax_id = taxes[0]["id"]
            _tax_id_cache[tax_rate] = tax_id
            frappe.logger().warning(
                f"Could not find matching tax rate for {tax_rate}%, using default: {taxes[0].get('name', 'unknown')}"
            )
            return tax_id

        return None

    except Exception as e:
        frappe.log_error(f"Failed to get tax ID for rate {tax_rate}%: {e}")
        return None


@temp_shopware_session
def get_or_create_delivery_time(client, delivery_time_name: str) -> Optional[str]:
    """
    Get existing or create new Delivery Time in Shopware.

    Shopware delivery times have a name and min/max values in a specific unit.
    Common examples: "1-3 days", "3-5 days", "1-2 weeks"

    Args:
        client: Shopware API client
        delivery_time_name: Name/label of the delivery time (e.g., "1-3 Werktage")

    Returns:
        Shopware delivery time ID if found/created, None otherwise
    """
    global _delivery_time_cache

    if delivery_time_name in _delivery_time_cache:
        return _delivery_time_cache[delivery_time_name]

    try:
        # Search for existing delivery time by name
        response = client.request_post(
            "search/delivery-time",
            {"filter": [{"type": "equals", "field": "name", "value": delivery_time_name}]}
        )
        delivery_times = response.get("data", [])

        if delivery_times:
            dt_id = delivery_times[0]["id"]
            _delivery_time_cache[delivery_time_name] = dt_id
            return dt_id

        # Parse delivery time name to extract min/max values
        import re
        
        name_lower = delivery_time_name.lower()
        
        # Handle "sofort" or "lagernd"
        if "sofort" in name_lower or "lagernd" in name_lower:
            min_val = 1
            max_val = 3
            unit = 'day'
        else:
            # Try range "1-3"
            match_range = re.search(r'(\d+)\s*[-–]\s*(\d+)', delivery_time_name)
            # Try single number "5 days"
            match_single = re.search(r'(\d+)', delivery_time_name)
            
            if match_range:
                min_val = int(match_range.group(1))
                max_val = int(match_range.group(2))
            elif match_single:
                val = int(match_single.group(1))
                min_val = val
                max_val = val
            else:
                # Default values if parsing fails
                min_val = 1
                max_val = 3

            # Determine unit (day, week, month, year)
            if 'woche' in name_lower or 'week' in name_lower:
                unit = 'week'
            elif 'monat' in name_lower or 'month' in name_lower:
                unit = 'month'
            elif 'jahr' in name_lower or 'year' in name_lower:
                unit = 'year'
            else:
                unit = 'day'  # Default to days

        # Create new delivery time
        dt_id = generate_uuid(f"delivery_time_{delivery_time_name}")
        payload = {
            "id": dt_id,
            "name": delivery_time_name,
            "min": min_val,
            "max": max_val,
            "unit": unit,
        }

        client.request_post("delivery-time", payload)
        _delivery_time_cache[delivery_time_name] = dt_id
        return dt_id

    except Exception as e:
        frappe.log_error(f"Failed to get/create DeliveryTime {delivery_time_name}: {e}")
        return None


# Cache for manufacturers
_manufacturer_cache: Dict[str, str] = {}


@temp_shopware_session
def get_or_create_manufacturer(client, manufacturer_name: str) -> Optional[str]:
    """
    Get existing or create new Manufacturer (Product Manufacturer) in Shopware.

    Args:
        client: Shopware API client
        manufacturer_name: Name of the manufacturer/brand

    Returns:
        Shopware manufacturer ID if found/created, None otherwise
    """
    global _manufacturer_cache

    if not manufacturer_name:
        return None

    if manufacturer_name in _manufacturer_cache:
        return _manufacturer_cache[manufacturer_name]

    try:
        # Search for existing manufacturer by name
        response = client.request_post(
            "search/product-manufacturer",
            {"filter": [{"type": "equals", "field": "name", "value": manufacturer_name}]}
        )
        manufacturers = response.get("data", [])

        if manufacturers:
            mfr_id = manufacturers[0]["id"]
            _manufacturer_cache[manufacturer_name] = mfr_id
            return mfr_id

        # Create new manufacturer
        mfr_id = generate_uuid(f"manufacturer_{manufacturer_name}")
        payload = {
            "id": mfr_id,
            "name": manufacturer_name,
        }

        client.request_post("product-manufacturer", payload)
        _manufacturer_cache[manufacturer_name] = mfr_id
        return mfr_id

    except Exception as e:
        frappe.log_error(f"Failed to get/create Manufacturer {manufacturer_name}: {e}")
        return None


def get_product_media_folder_id(client) -> Optional[str]:
    """Get the Product Media folder ID in Shopware."""
    global _media_folder_cache

    if "product" in _media_folder_cache:
        return _media_folder_cache["product"]

    try:
        # Search for Product Media folder
        response = client.request_post(
            "search/media-folder",
            {"filter": [{"type": "equals", "field": "name", "value": "Product Media"}]}
        )
        folders = response.get("data", [])

        if folders:
            folder_id = folders[0]["id"]
            _media_folder_cache["product"] = folder_id
            return folder_id

        # If not found, search for any default folder
        response = client.request_post(
            "search/media-default-folder",
            {"filter": [{"type": "equals", "field": "entity", "value": "product"}]}
        )
        default_folders = response.get("data", [])
        if default_folders:
            folder_id = default_folders[0].get("folderId")
            if folder_id:
                _media_folder_cache["product"] = folder_id
                return folder_id

        return None

    except Exception as e:
        frappe.log_error(f"Failed to get Product Media folder: {e}")
        return None


def get_file_content_and_type(file_url: str) -> Tuple[Optional[bytes], Optional[str], Optional[str]]:
    """
    Get file content and mime type from ERPNext file URL.

    Args:
        file_url: ERPNext file URL (e.g., /files/image.jpg or /private/files/image.jpg)

    Returns:
        Tuple of (file_content, mime_type, extension) or (None, None, None) if failed
    """
    try:
        if not file_url:
            return None, None, None

        # Handle different file URL formats
        if file_url.startswith(('http://', 'https://')):
            # External URL - download it
            response = requests.get(file_url, timeout=30)
            if response.status_code == 200:
                content_type = response.headers.get('Content-Type', 'image/jpeg')
                ext = mimetypes.guess_extension(content_type) or '.jpg'
                return response.content, content_type, ext.lstrip('.')
            return None, None, None

        # Local file
        if file_url.startswith('/private/files/'):
            file_path = os.path.join(frappe.get_site_path(), file_url.lstrip('/'))
        elif file_url.startswith('/files/'):
            file_path = os.path.join(frappe.get_site_path(), 'public', file_url.lstrip('/'))
        else:
            # Assume it's in public files
            file_path = os.path.join(get_files_path(), file_url.lstrip('/'))

        if os.path.exists(file_path):
            with open(file_path, 'rb') as f:
                content = f.read()

            mime_type, _ = mimetypes.guess_type(file_path)
            mime_type = mime_type or 'image/jpeg'
            ext = os.path.splitext(file_path)[1].lstrip('.') or 'jpg'

            return content, mime_type, ext

        return None, None, None

    except Exception as e:
        frappe.log_error(f"Failed to get file content for {file_url}: {e}")
        return None, None, None


def upload_media_to_shopware(client, file_url: str, item_code: str, position: int = 0) -> Optional[str]:
    """
    Upload a media file to Shopware and return the media ID.

    Steps:
    1. Create media entity
    2. Upload file content to media

    Args:
        client: Shopware API client
        file_url: ERPNext file URL
        item_code: Item code (for naming)
        position: Image position (for unique naming)

    Returns:
        media_id if successful, None otherwise
    """
    try:
        # Get file content
        file_content, mime_type, extension = get_file_content_and_type(file_url)
        if not file_content:
            frappe.log_error(f"Could not read file: {file_url}")
            return None

        # Get product media folder
        folder_id = get_product_media_folder_id(client)

        # Generate deterministic media ID
        media_id = generate_uuid(f"media_{item_code}_{position}")

        # Step 1: Check if media already exists, if not create it
        media_exists = False
        try:
            response = client.request_get(f"media/{media_id}")
            if response.get("data"):
                media_exists = True
        except BaseException:
            # Media doesn't exist (404), we'll create it
            # Note: ShopwareAPIError inherits from BaseException, not Exception
            pass

        if not media_exists:
            media_payload = {
                "id": media_id,
            }
            if folder_id:
                media_payload["mediaFolderId"] = folder_id

            try:
                client.request_post("media", media_payload)
            except BaseException as e:
                # Media might already exist (race condition or different error format)
                # Note: ShopwareAPIError inherits from BaseException, not Exception
                error_str = str(e).lower()
                if "already exists" not in error_str and "updatecommand" not in error_str:
                    frappe.log_error(f"Failed to create media entity for {item_code}: {e}")
                    return None

        # Step 2: Upload file content (only if media doesn't have file yet)
        # Check if media already has a file uploaded
        try:
            media_check = client.request_get(f"media/{media_id}")
            media_data = media_check.get("data", {})
            # If media has a fileName, it already has content uploaded
            if media_data.get("fileName"):
                return media_id
        except BaseException as check_error:
            # If we can't check the media, it might not exist - try to create it again or skip
            # Note: ShopwareAPIError inherits from BaseException, not Exception
            frappe.log_error(f"Media check failed for {item_code}, media_id {media_id}: {check_error}")
            return None

        # Generate filename (sanitize to remove illegal characters like |)
        filename = sanitize_filename(f"{item_code}_{position}")

        # Upload using _action/media/{mediaId}/upload endpoint
        upload_url = f"_action/media/{media_id}/upload"

        try:
            # The SDK supports octet-stream content type for binary uploads
            client.request_post(
                upload_url,
                payload=file_content,
                content_type="octet-stream",
                additional_query_params={
                    "extension": extension,
                    "fileName": filename
                }
            )
        except BaseException as upload_error:
            # If upload fails with "file already exists" error, that's OK
            # Note: ShopwareAPIError inherits from BaseException, not Exception
            error_str = str(upload_error).lower()
            if "already exists" not in error_str and "duplicate" not in error_str:
                frappe.log_error(f"Failed to upload file content for {item_code}: {upload_error}")
                return None

        return media_id

    except BaseException as e:
        # Catch all exceptions including ShopwareAPIError (inherits from BaseException)
        frappe.log_error(f"Failed to upload media for {item_code}: {e}")
        return None


def get_file_hash(file_url: str) -> Optional[str]:
    """
    Calculate MD5 hash of a file to detect duplicates.

    Args:
        file_url: ERPNext file URL

    Returns:
        MD5 hash string or None if file cannot be read
    """
    try:
        if not file_url:
            return None

        # Determine file path
        if file_url.startswith(('http://', 'https://')):
            # External URL - skip hash check for now
            return None

        if file_url.startswith('/private/files/'):
            file_path = os.path.join(frappe.get_site_path(), file_url.lstrip('/'))
        elif file_url.startswith('/files/'):
            file_path = os.path.join(frappe.get_site_path(), 'public', file_url.lstrip('/'))
        else:
            file_path = os.path.join(get_files_path(), file_url.lstrip('/'))

        if os.path.exists(file_path):
            with open(file_path, 'rb') as f:
                return hashlib.md5(f.read()).hexdigest()

        return None
    except Exception:
        return None


def get_item_images(item) -> List[str]:
    """
    Get all unique image URLs for an item.

    Returns list of file URLs, with main image first.
    Filters out duplicate images based on file content hash.
    """
    images = []
    seen_hashes = set()

    # Main image first
    if item.image:
        main_hash = get_file_hash(item.image)
        images.append(item.image)
        if main_hash:
            seen_hashes.add(main_hash)

    # Get all attachments that are images
    attachments = frappe.get_all(
        "File",
        filters={
            "attached_to_doctype": "Item",
            "attached_to_name": item.name,
            "is_folder": 0,
        },
        fields=["file_url", "file_name"],
        order_by="creation asc"
    )

    image_extensions = ('.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp')

    for attachment in attachments:
        file_url = attachment.file_url
        if file_url and file_url.lower().endswith(image_extensions):
            # Skip if URL already added
            if file_url in images:
                continue

            # Check content hash to detect duplicate images with different filenames
            file_hash = get_file_hash(file_url)
            if file_hash:
                if file_hash in seen_hashes:
                    # Skip duplicate content
                    frappe.logger().debug(
                        f"Skipping duplicate image {file_url} for {item.name} (same content as existing image)"
                    )
                    continue
                seen_hashes.add(file_hash)

            images.append(file_url)

    return images


def sync_product_images_to_shopware(client, item, shopware_product_id: str) -> bool:
    """
    Sync all images from ERPNext Item to Shopware product.

    Uses delta-sync: only uploads images if content has changed (hash-based).
    This eliminates unnecessary API calls and dramatically speeds up bulk syncs.

    Args:
        client: Shopware API client
        item: ERPNext Item document
        shopware_product_id: Shopware product UUID

    Returns:
        True if successful
    """
    try:
        images = get_item_images(item)
        if not images:
            return True  # No images to sync

        product_media_list = []
        cover_id = None
        images_uploaded = 0
        images_skipped = 0

        for position, image_url in enumerate(images):
            # Delta-sync: Check if image content has changed
            current_hash = get_file_hash(image_url)
            cached_hash = get_cached_image_hash(item.item_code, position)

            # If hash matches, image hasn't changed - skip upload but still include in media list
            if current_hash and cached_hash and current_hash == cached_hash:
                # Image unchanged - use existing media ID without re-uploading
                media_id = generate_uuid(f"media_{item.item_code}_{position}")
                images_skipped += 1
            else:
                # Image is new or changed - upload it
                media_id = upload_media_to_shopware(client, image_url, item.item_code, position)

                # Cache the hash for future delta-sync
                if media_id and current_hash:
                    set_cached_image_hash(item.item_code, position, current_hash)
                    images_uploaded += 1

            if media_id:
                # Create product-media relationship ID
                product_media_id = generate_uuid(f"product_media_{item.item_code}_{position}")

                product_media_list.append({
                    "id": product_media_id,
                    "mediaId": media_id,
                    "position": position,
                })

                # First image is the cover
                if position == 0:
                    cover_id = product_media_id

        if product_media_list:
            # Update product with media
            update_payload = {
                "media": product_media_list,
            }
            if cover_id:
                update_payload["coverId"] = cover_id

            client.request_patch(f"product/{shopware_product_id}", update_payload)

            if images_uploaded > 0 or images_skipped > 0:
                frappe.logger().info(
                    f"Image sync for {item.item_code}: {images_uploaded} uploaded, {images_skipped} skipped (unchanged)"
                )

        return True

    except Exception as e:
        frappe.log_error(f"Failed to sync images for {item.name}: {e}")
        return False


def map_erpnext_item_to_shopware(erpnext_item) -> Dict[str, Any]:
    """
    Map ERPNext Item fields to Shopware product payload.

    Returns a dict suitable for Shopware product API.
    """
    # Get description - prefer web_long_description (rich HTML for websites),
    # then fall back to description, then item_name
    description = (
        getattr(erpnext_item, 'web_long_description', None) or
        erpnext_item.description or
        erpnext_item.item_name
    )

    payload = {
        "productNumber": erpnext_item.item_code,
        "name": erpnext_item.item_name,
        "description": description,
        "active": not erpnext_item.disabled,
        "stock": 0,  # Stock is managed separately
        "isCloseout": False,
    }

    # Price (gross price in default currency)
    price = flt(erpnext_item.get(ITEM_SELLING_RATE_FIELD) or 0)
    
    # Shopware requires price field always. For templates without price, try to find lowest variant price.
    if price <= 0 and hasattr(erpnext_item, "has_variants") and erpnext_item.has_variants:
        try:
            # Find cheapest variant price
            variant_price = frappe.db.sql("""
                SELECT price_list_rate FROM `tabItem Price`
                WHERE item_code IN (SELECT name FROM `tabItem` WHERE variant_of = %s AND disabled = 0)
                AND price_list_rate > 0
                ORDER BY price_list_rate ASC
                LIMIT 1
            """, erpnext_item.name)
            
            if variant_price and variant_price[0][0]:
                price = flt(variant_price[0][0])
            else:
                price = 0.01  # Ultimate fallback
        except Exception:
            price = 0.01

    # Always include price field for Shopware
    # Price from ERPNext is typically the net price (without tax)
    # Shopware expects gross and net prices separately
    # Default tax rate is 19% (German standard VAT)
    gross_price = price if price > 0 else 0.01
    net_price = price if price > 0 else 0.01

    # If the item has a tax template, try to get the tax rate
    tax_rate = 19.0  # Default German VAT
    if hasattr(erpnext_item, 'taxes') and erpnext_item.taxes:
        for tax_row in erpnext_item.taxes:
            if tax_row.item_tax_template:
                # Get the tax rate from the template
                template_tax_rate = frappe.db.get_value(
                    "Item Tax Template Detail",
                    {"parent": tax_row.item_tax_template},
                    "tax_rate"
                )
                if template_tax_rate:
                    tax_rate = flt(template_tax_rate)
                    break

    # Calculate gross from net (ERPNext prices are typically net)
    # gross = net * (1 + tax_rate/100)
    if price > 0:
        gross_price = round(net_price * (1 + tax_rate / 100), 2)

    payload["price"] = [{
        "currencyId": None,  # Will be set to default currency
        "gross": gross_price,
        "net": net_price,
        "linked": False,  # Not linked since we calculate separately
    }]

    # Store the tax rate for later use when selecting the Shopware tax ID
    payload["_tax_rate"] = tax_rate

    # Weight
    weight = flt(erpnext_item.weight_per_unit or 0)
    if weight > 0:
        payload["weight"] = weight

    # Dimensions (convert cm to mm for Shopware)
    if hasattr(erpnext_item, 'item_height') and erpnext_item.item_height:
        payload["height"] = flt(erpnext_item.item_height) * 10  # cm to mm
    if hasattr(erpnext_item, 'item_width') and erpnext_item.item_width:
        payload["width"] = flt(erpnext_item.item_width) * 10
    if hasattr(erpnext_item, 'item_length') and erpnext_item.item_length:
        payload["length"] = flt(erpnext_item.item_length) * 10

    # SEO fields
    if hasattr(erpnext_item, 'seo_title') and erpnext_item.seo_title:
        payload["metaTitle"] = erpnext_item.seo_title
    if hasattr(erpnext_item, 'seo_meta_description') and erpnext_item.seo_meta_description:
        payload["metaDescription"] = erpnext_item.seo_meta_description
    if hasattr(erpnext_item, 'seo_keywords') and erpnext_item.seo_keywords:
        payload["keywords"] = erpnext_item.seo_keywords

    # Delivery time - will be set separately via get_or_create_delivery_time
    # The deliveryTimeId is added in upload_erpnext_item_to_shopware

    # EAN/GTIN
    if erpnext_item.barcodes:
        for barcode in erpnext_item.barcodes:
            if barcode.barcode:
                payload["ean"] = barcode.barcode
                break

    return payload


def get_actual_variant_values(template_item_code: str, attribute_name: str) -> List[str]:
    """
    Get unique attribute values actually used by enabled variants of a template.
    """
    # Get all enabled variants
    variants = frappe.get_all(
        "Item",
        filters={"variant_of": template_item_code, "disabled": 0},
        pluck="name"
    )
    
    if not variants:
        return []

    # Get values from Item Variant Attribute table
    values = frappe.get_all(
        "Item Variant Attribute",
        filters={
            "parent": ["in", variants],
            "attribute": attribute_name
        },
        pluck="attribute_value",
        distinct=1
    )
    
    return [v for v in values if v]


def get_item_properties(erpnext_item) -> List[Dict[str, str]]:
    """
    Get property values for Shopware from ERPNext Item.

    Priority:
    1. Read from shopware_properties table (new flexible key-value table)
    2. Fallback to old jattr_* custom fields (for backwards compatibility)

    Returns list of {group_name, option_value, filterable} dicts.
    """
    properties = []

    # Priority 1: Read from new shopware_properties table
    properties_table = getattr(erpnext_item, 'shopware_properties', None) or []
    for row in properties_table:
        if row.property_type == 'Property' and row.property_value:
            properties.append({
                "group_name": row.property_name,
                "option_value": cstr(row.property_value).strip(),
                "filterable": bool(row.filterable) if hasattr(row, 'filterable') else True
            })

    # Priority 2: Fallback to old configurable mappings (for backwards compatibility)
    if not properties:
        mappings = get_field_mappings_cached()
        for erpnext_field, config in mappings.items():
            if config['mapping_type'] == 'Property':
                value = getattr(erpnext_item, erpnext_field, None)
                if value:
                    properties.append({
                        "group_name": config['labels'].get('de-DE', erpnext_field),
                        "option_value": cstr(value).strip(),
                        "filterable": config.get('filterable', True)
                    })

    return properties


@temp_shopware_session
def upload_template_item_to_shopware(client, template_item) -> Optional[str]:
    """
    Export an ERPNext template item (has_variants=1) to Shopware as a parent product.

    Creates the parent product with configuratorSettings for all variant options.

    Args:
        client: Shopware API client
        template_item: ERPNext Item document with has_variants=1

    Returns:
        Shopware product ID if successful, None otherwise
    """
    setting = frappe.get_cached_doc(SETTING_DOCTYPE)

    # Check if already synced
    shopware_product_id = get_shopware_document_id("Item", template_item.name)
    if shopware_product_id:
        return shopware_product_id

    try:
        # Build base product payload
        product_payload = map_erpnext_item_to_shopware(template_item)

        # Generate product ID
        product_id = generate_uuid(f"product_{template_item.item_code}")
        product_payload["id"] = product_id

        # Get default currency ID (cached - eliminates 1 API call per sync)
        currency_id = get_cached_currency_id(client, "EUR")
        if currency_id and product_payload.get("price"):
            product_payload["price"][0]["currencyId"] = currency_id

        # Get/create categories (supports multi-category via Webshop's Website Item Groups)
        category_ids = sync_all_item_categories.__wrapped__(client, template_item.item_code)
        if category_ids:
            product_payload["categories"] = category_ids
        elif template_item.item_group:
            # Fallback to single category if no categories found
            category_id = sync_category_hierarchy.__wrapped__(client, template_item.item_group)
            if category_id:
                product_payload["categories"] = [{"id": category_id}]

        # Get sales channel for visibility (cached - eliminates 1 API call per sync)
        sales_channel_id = get_cached_sales_channel_id(client)
        if sales_channel_id:
            product_payload["visibilities"] = [{
                "salesChannelId": sales_channel_id,
                "visibility": 30
            }]

        # Get tax ID based on tax rate from product payload
        tax_rate = product_payload.pop("_tax_rate", 19.0)
        tax_id = get_tax_id_by_rate.__wrapped__(client, tax_rate)
        if tax_id:
            product_payload["taxId"] = tax_id

        # Get/create manufacturer
        manufacturer_name = None
        if hasattr(template_item, 'brand') and template_item.brand:
            manufacturer_name = template_item.brand
        elif hasattr(template_item, 'default_item_manufacturer') and template_item.default_item_manufacturer:
            manufacturer_name = template_item.default_item_manufacturer

        if manufacturer_name:
            manufacturer_id = get_or_create_manufacturer.__wrapped__(client, manufacturer_name)
            if manufacturer_id:
                product_payload["manufacturerId"] = manufacturer_id

        # Build configuratorSettings from Item Attributes
        # These define which options are available for variant selection
        configurator_settings = []
        attributes = get_template_item_attributes(template_item)
        seen_option_ids = set()

        for attr in attributes:
            attr_name = attr["name"]
            
            # Get only values actually used by variants
            actual_values = get_actual_variant_values(template_item.name, attr_name)
            
            if not actual_values:
                continue

            # Get or create property group for this attribute
            group_id = get_or_create_property_group.__wrapped__(client, attr_name)
            if not group_id:
                continue

            # Create configurator setting for each possible value
            for value in actual_values:
                option_id = get_or_create_variant_option.__wrapped__(client, group_id, attr_name, value)
                if option_id and option_id not in seen_option_ids:
                    seen_option_ids.add(option_id)
                    config_setting_id = generate_uuid(f"config_{template_item.item_code}_{attr_name}_{value}")
                    configurator_settings.append({
                        "id": config_setting_id,
                        "optionId": option_id,
                    })

        if configurator_settings:
            product_payload["configuratorSettings"] = configurator_settings

        # Check if product already exists in Shopware (e.g. from broken sync)
        product_exists = False
        try:
            client.request_get(f"product/{product_id}")
            product_exists = True
        except BaseException:
            pass

        if product_exists:
            # Remove visibilities from PATCH to avoid duplicate entry error
            # (visibilities are only needed when creating, not updating)
            product_payload.pop("visibilities", None)
            client.request_patch(f"product/{product_id}", product_payload)
            frappe.logger().info(f"Template product {template_item.item_code} already exists in Shopware, updated instead of created.")
        else:
            # Create the parent product
            client.request_post("product", product_payload)

        # Create Ecommerce Item link
        if not get_shopware_document_id("Item", template_item.name):
            frappe.get_doc({
                "doctype": "Ecommerce Item",
            "integration": MODULE_NAME,
            "erpnext_item_code": template_item.name,
            "integration_item_code": product_id,
            "has_variants": 1,
            "sku": template_item.item_code,
        }).insert(ignore_permissions=True)

        create_shopware_log(
            status="Success",
            request_data=product_payload,
            message=f"Created template product: {template_item.name} -> {product_id}",
            method="upload_template_item_to_shopware"
        )

        # Sync images for template
        if getattr(setting, 'sync_images_to_shopware', True):
            sync_product_images_to_shopware(client, template_item, product_id)

        return product_id

    except Exception as e:
        create_shopware_log(
            status="Error",
            message=f"Failed to upload template {template_item.name}: {str(e)}",
            exception=str(e),
            method="upload_template_item_to_shopware",
            rollback=True
        )
        frappe.log_error(f"Shopware template product upload failed for {template_item.name}: {e}")
        return None


@temp_shopware_session
def upload_variant_item_to_shopware(client, variant_item, parent_shopware_id: str) -> Optional[str]:
    """
    Export an ERPNext variant item to Shopware as a child product.

    Args:
        client: Shopware API client
        variant_item: ERPNext Item document (variant_of is set)
        parent_shopware_id: Shopware product ID of the parent/template product

    Returns:
        Shopware product ID if successful, None otherwise
    """
    setting = frappe.get_cached_doc(SETTING_DOCTYPE)

    # Check if already synced
    shopware_product_id = get_shopware_document_id("Item", variant_item.name)
    if shopware_product_id:
        return shopware_product_id

    # Verify parent exists in Shopware before creating variant
    try:
        client.request_get(f"product/{parent_shopware_id}")
    except BaseException as e:  # ShopwareAPIError inherits from BaseException, not Exception
        # Parent doesn't exist in Shopware - need to recreate it
        frappe.log_error(
            f"Parent product {parent_shopware_id} not found in Shopware for variant {variant_item.name}. "
            f"Attempting to recreate parent.",
            "Shopware Variant Sync"
        )
        # Delete stale Ecommerce Item link and recreate parent
        template_item = frappe.get_doc("Item", variant_item.variant_of)
        frappe.db.delete("Ecommerce Item", {
            "integration": MODULE_NAME,
            "erpnext_item_code": template_item.name
        })
        frappe.db.commit()
        # Recreate parent
        parent_shopware_id = upload_template_item_to_shopware.__wrapped__(client, template_item)
        if not parent_shopware_id:
            frappe.log_error(
                f"Failed to recreate parent for variant {variant_item.name}",
                "Shopware Variant Sync"
            )
            return None

    try:
        # Build variant product payload
        product_payload = map_erpnext_item_to_shopware(variant_item)

        # Generate variant product ID
        product_id = generate_uuid(f"product_{variant_item.item_code}")
        product_payload["id"] = product_id

        # Set parent ID - this makes it a variant
        product_payload["parentId"] = parent_shopware_id

        # Get default currency ID
        currency_resp = client.request_post(
            "search/currency",
            {"filter": [{"type": "equals", "field": "isoCode", "value": "EUR"}], "limit": 1}
        )
        currencies = currency_resp.get("data", [])
        if currencies and product_payload.get("price"):
            product_payload["price"][0]["currencyId"] = currencies[0]["id"]

        # Get tax ID based on tax rate from product payload
        tax_rate = product_payload.pop("_tax_rate", 19.0)
        tax_id = get_tax_id_by_rate.__wrapped__(client, tax_rate)
        if tax_id:
            product_payload["taxId"] = tax_id

        # Get/create delivery time if specified (important for variants too!)
        if hasattr(variant_item, 'delivery_time') and variant_item.delivery_time:
            delivery_time_id = get_or_create_delivery_time.__wrapped__(client, variant_item.delivery_time)
            if delivery_time_id:
                product_payload["deliveryTimeId"] = delivery_time_id

        # Get/create manufacturer if specified (brand has priority over default_item_manufacturer)
        manufacturer_name = None
        if hasattr(variant_item, 'brand') and variant_item.brand:
            manufacturer_name = variant_item.brand
        elif hasattr(variant_item, 'default_item_manufacturer') and variant_item.default_item_manufacturer:
            manufacturer_name = variant_item.default_item_manufacturer

        if manufacturer_name:
            manufacturer_id = get_or_create_manufacturer.__wrapped__(client, manufacturer_name)
            if manufacturer_id:
                product_payload["manufacturerId"] = manufacturer_id

        # Build options from variant attributes
        # These specify which option values this variant has
        options = []
        attr_values = get_variant_attribute_values(variant_item)

        for attr_name, attr_value in attr_values.items():
            # Skip empty attribute values to avoid Shopware API errors
            if not attr_value or not cstr(attr_value).strip():
                frappe.logger().warning(
                    f"Skipping empty attribute value for {attr_name} on variant {variant_item.name}"
                )
                continue

            # Get the property group
            group_id = get_or_create_property_group.__wrapped__(client, attr_name)
            if not group_id:
                continue

            # Get the option
            option_id = get_or_create_variant_option.__wrapped__(client, group_id, attr_name, attr_value)
            if option_id:
                options.append({"id": option_id})

        if options:
            product_payload["options"] = options

        # Remove fields that should be inherited from parent
        # (category, visibility are typically inherited)
        product_payload.pop("categories", None)
        product_payload.pop("visibilities", None)

        # Check if variant already exists
        variant_exists = False
        try:
            client.request_get(f"product/{product_id}")
            variant_exists = True
        except BaseException:
            pass

        if variant_exists:
            client.request_patch(f"product/{product_id}", product_payload)
        else:
            # Create the variant product - with fallback to PATCH if already exists
            try:
                client.request_post("product", product_payload)
            except BaseException as e:
                if "WRITE_TYPE_INTEND_ERROR" in str(e):
                    # Product already exists in Shopware, use PATCH instead
                    client.request_patch(f"product/{product_id}", product_payload)
                else:
                    raise

        # Create Ecommerce Item link
        if not get_shopware_document_id("Item", variant_item.name):
            frappe.get_doc({
                "doctype": "Ecommerce Item",
            "integration": MODULE_NAME,
            "erpnext_item_code": variant_item.name,
            "integration_item_code": product_id,
            "has_variants": 0,
            "variant_id": product_id,
            "sku": variant_item.item_code,
        }).insert(ignore_permissions=True)

        create_shopware_log(
            status="Success",
            request_data=product_payload,
            message=f"Created variant product: {variant_item.name} -> {product_id}",
            method="upload_variant_item_to_shopware"
        )

        # Sync custom fields for variant (Zubehör, Sicherheitsblätter, Produktblätter)
        custom_fields = get_item_custom_fields(variant_item)
        if custom_fields:
            # Ensure custom field set exists in Shopware
            ensure_shopware_custom_field_set.__wrapped__(client)

            # Update variant with custom fields
            client.request_patch(
                f"product/{product_id}",
                {"customFields": custom_fields}
            )

        # Sync properties (jattr_* fields) for variant
        properties = get_item_properties(variant_item)
        if properties:
            property_option_ids = []

            for prop in properties:
                group_id = get_or_create_property_group.__wrapped__(client, prop["group_name"])
                if group_id:
                    option_id = get_or_create_property_option.__wrapped__(
                        client, group_id, prop["group_name"], prop["option_value"]
                    )
                    if option_id:
                        property_option_ids.append({"id": option_id})

            if property_option_ids:
                client.request_patch(
                    f"product/{product_id}",
                    {"properties": property_option_ids}
                )

        # Sync images for variant
        if getattr(setting, 'sync_images_to_shopware', True):
            sync_product_images_to_shopware(client, variant_item, product_id)

        return product_id

    except Exception as e:
        create_shopware_log(
            status="Error",
            message=f"Failed to upload variant {variant_item.name}: {str(e)}",
            exception=str(e),
            method="upload_variant_item_to_shopware",
            rollback=True
        )
        frappe.log_error(f"Shopware variant product upload failed for {variant_item.name}: {e}")
        return None


@temp_shopware_session
def upload_erpnext_item_to_shopware(client, doc, method=None):
    """
    Upload/update ERPNext Item to Shopware.

    This is the main export function, similar to Shopify's upload_erpnext_item.
    Handles:
    - Simple items (no variants)
    - Template items (has_variants=1) - creates parent product with configuratorSettings
    - Variant items (variant_of is set) - creates child product with parentId and options
    """
    item = doc

    # Skip if this came from integration
    if item.flags.from_integration:
        return

    setting = frappe.get_cached_doc(SETTING_DOCTYPE)

    if not setting.is_enabled():
        return

    if frappe.flags.in_import:
        return

    # Check if already synced FIRST to determine if this is a new product or update
    shopware_product_id = get_shopware_document_id("Item", item.name)
    is_new_product = not bool(shopware_product_id)

    # For NEW products: check upload_erpnext_items setting
    # For EXISTING products: check update_shopware_item_on_update setting
    if is_new_product:
        if not getattr(setting, 'upload_erpnext_items', False):
            return
    else:
        if not getattr(setting, 'update_shopware_item_on_update', True):
            return

    # Handle template items with variants
    if item.has_variants:
        # Export template item as parent product
        upload_template_item_to_shopware.__wrapped__(client, item)
        return

    # Handle variant items
    if item.variant_of:
        # First ensure the parent/template is exported
        template_item = frappe.get_doc("Item", item.variant_of)
        parent_shopware_id = get_shopware_document_id("Item", template_item.name)

        if not parent_shopware_id:
            # Create parent first
            parent_shopware_id = upload_template_item_to_shopware.__wrapped__(client, template_item)

        if parent_shopware_id:
            # Now create the variant
            upload_variant_item_to_shopware.__wrapped__(client, item, parent_shopware_id)
        return

    # Note: shopware_product_id and is_new_product are already set above (line 2088-2090)

    try:
        # Build product payload
        product_payload = map_erpnext_item_to_shopware(item)

        # Get default currency ID (cached - eliminates 1 API call per sync)
        currency_id = get_cached_currency_id(client, "EUR")
        if currency_id and product_payload.get("price"):
            product_payload["price"][0]["currencyId"] = currency_id

        # Get/create categories (supports multi-category via Webshop's Website Item Groups)
        category_ids = sync_all_item_categories.__wrapped__(client, item.item_code)
        if category_ids:
            product_payload["categories"] = category_ids
        elif item.item_group:
            # Fallback to single category if no categories found
            category_id = sync_category_hierarchy.__wrapped__(client, item.item_group)
            if category_id:
                product_payload["categories"] = [{"id": category_id}]

        # Get sales channel for visibility (cached - eliminates 1 API call per sync)
        sales_channel_id = get_cached_sales_channel_id(client)
        if sales_channel_id:
            product_payload["visibilities"] = [{
                "salesChannelId": sales_channel_id,
                "visibility": 30  # All (search + listing)
            }]

        # Get tax ID based on tax rate from product payload
        tax_rate = product_payload.pop("_tax_rate", 19.0)
        tax_id = get_tax_id_by_rate.__wrapped__(client, tax_rate)
        if tax_id:
            product_payload["taxId"] = tax_id

        # Get/create delivery time if specified
        if hasattr(item, 'delivery_time') and item.delivery_time:
            delivery_time_id = get_or_create_delivery_time.__wrapped__(client, item.delivery_time)
            if delivery_time_id:
                product_payload["deliveryTimeId"] = delivery_time_id

        # Get/create manufacturer if specified (brand has priority over default_item_manufacturer)
        manufacturer_name = None
        if hasattr(item, 'brand') and item.brand:
            manufacturer_name = item.brand
        elif hasattr(item, 'default_item_manufacturer') and item.default_item_manufacturer:
            manufacturer_name = item.default_item_manufacturer

        if manufacturer_name:
            manufacturer_id = get_or_create_manufacturer.__wrapped__(client, manufacturer_name)
            if manufacturer_id:
                product_payload["manufacturerId"] = manufacturer_id

        if is_new_product:
            # Create new product
            product_id = generate_uuid(f"product_{item.item_code}")
            product_payload["id"] = product_id

            # Check if product already exists in Shopware
            product_exists = False
            try:
                client.request_get(f"product/{product_id}")
                product_exists = True
            except BaseException:
                pass

            if product_exists:
                # Remove visibilities from PATCH to avoid duplicate entry error
                product_payload.pop("visibilities", None)
                client.request_patch(f"product/{product_id}", product_payload)
                frappe.logger().info(f"Product {item.item_code} already exists in Shopware, updated instead of created.")
            else:
                client.request_post("product", product_payload)

            # Link in Ecommerce Item
            if not get_shopware_document_id("Item", item.name):
                frappe.get_doc({
                    "doctype": "Ecommerce Item",
                "integration": MODULE_NAME,
                "erpnext_item_code": item.name,
                "integration_item_code": product_id,
                "has_variants": 0,
                "sku": item.item_code,
            }).insert(ignore_permissions=True)

            shopware_product_id = product_id

            create_shopware_log(
                status="Success",
                request_data=product_payload,
                message=f"Created product: {item.name} -> {product_id}",
                method="upload_erpnext_item_to_shopware"
            )
        else:
            # Update existing product
            # Remove visibilities from PATCH to avoid duplicate entry error
            # (visibilities are only needed for initial product creation)
            product_payload.pop("visibilities", None)

            # Clear existing categories before patching to ensure replacement
            # (Shopware PATCH adds categories instead of replacing)
            if product_payload.get("categories"):
                clear_product_categories(client, shopware_product_id)

            client.request_patch(f"product/{shopware_product_id}", product_payload)

            if getattr(setting, 'update_shopware_item_on_update', True):
                create_shopware_log(
                    status="Success",
                    request_data=product_payload,
                    message=f"Updated product: {item.name}",
                    method="upload_erpnext_item_to_shopware"
                )

        # Sync properties
        properties = get_item_properties(item)
        if properties:
            property_option_ids = []

            for prop in properties:
                group_id = get_or_create_property_group.__wrapped__(client, prop["group_name"])
                if group_id:
                    option_id = get_or_create_property_option.__wrapped__(
                        client, group_id, prop["group_name"], prop["option_value"]
                    )
                    if option_id:
                        property_option_ids.append({"id": option_id})

            if property_option_ids:
                client.request_patch(
                    f"product/{shopware_product_id}",
                    {"properties": property_option_ids}
                )

        # Sync custom fields (Zubehör, Sicherheitsblätter, Produktblätter)
        custom_fields = get_item_custom_fields(item)
        if custom_fields:
            # Ensure custom field set exists in Shopware
            ensure_shopware_custom_field_set.__wrapped__(client)

            # Update product with custom fields
            client.request_patch(
                f"product/{shopware_product_id}",
                {"customFields": custom_fields}
            )

        # Sync images (only if enabled in settings)
        if getattr(setting, 'sync_images_to_shopware', True):
            sync_product_images_to_shopware(client, item, shopware_product_id)

    except Exception as e:
        create_shopware_log(
            status="Error",
            message=f"Failed to upload {item.name}: {str(e)}",
            exception=str(e),
            method="upload_erpnext_item_to_shopware",
            rollback=True
        )
        frappe.log_error(f"Shopware product upload failed for {item.name}: {e}")


@frappe.whitelist()
def sync_item_to_shopware(item_code: str) -> Dict[str, Any]:
    """
    Manually sync a single item to Shopware.

    Args:
        item_code: ERPNext Item code

    Returns:
        dict: Sync result
    """
    try:
        item = frappe.get_doc("Item", item_code)
        upload_erpnext_item_to_shopware(doc=item)
        return {"success": True, "message": f"Synced {item_code} to Shopware"}
    except Exception as e:
        return {"success": False, "message": str(e)}


@frappe.whitelist()
def sync_template_with_variants_to_shopware(template_item_code: str) -> Dict[str, Any]:
    """
    Sync a template item and all its variants to Shopware.

    This creates the parent product with configuratorSettings and all child
    products with proper parentId and options.

    Args:
        template_item_code: ERPNext Item code of a template item (has_variants=1)

    Returns:
        dict: Sync result with counts
    """
    try:
        template_item = frappe.get_doc("Item", template_item_code)

        if not template_item.has_variants:
            return {"success": False, "message": f"{template_item_code} is not a template item (has_variants=0)"}

        # Sync template first
        upload_erpnext_item_to_shopware(doc=template_item)
        template_synced = 1

        # Get all variants
        variants = frappe.get_all(
            "Item",
            filters={"variant_of": template_item_code, "disabled": 0},
            pluck="name"
        )

        variants_synced = 0
        variant_errors = 0

        for variant_code in variants:
            try:
                variant_item = frappe.get_doc("Item", variant_code)
                upload_erpnext_item_to_shopware(doc=variant_item)
                variants_synced += 1
            except Exception as e:
                variant_errors += 1
                frappe.log_error(f"Failed to sync variant {variant_code}: {e}", "Shopware Variant Sync Error")

        return {
            "success": True,
            "message": f"Synced template {template_item_code} with {variants_synced} variants",
            "template_synced": template_synced,
            "variants_synced": variants_synced,
            "variant_errors": variant_errors,
            "total_variants": len(variants)
        }

    except Exception as e:
        return {"success": False, "message": str(e)}


@frappe.whitelist()
def bulk_sync_items_to_shopware(limit: int = 100, batch_size: int = 20, include_variants: bool = True) -> Dict[str, Any]:
    """
    Bulk sync items to Shopware in batches.

    Syncs items that are not yet linked to Shopware.
    Image sync is controlled by the 'sync_images_to_shopware' setting.

    When include_variants is True (default), this function will:
    1. First sync all template items (has_variants=1) as parent products
    2. Then sync their variants as child products
    3. Finally sync simple items (no variants)

    Args:
        limit: Maximum number of items to sync
        batch_size: Number of items to process in each batch (for progress tracking)
        include_variants: Whether to include template items and their variants

    Returns:
        dict: Sync results
    """
    setting = frappe.get_cached_doc(SETTING_DOCTYPE)

    if not setting.is_enabled():
        return {"success": False, "message": "Shopware integration is not enabled"}

    # Get items not yet synced
    synced_items = frappe.get_all(
        "Ecommerce Item",
        filters={"integration": MODULE_NAME},
        pluck="erpnext_item_code"
    )

    synced = 0
    errors = 0
    batch_size = cint(batch_size) or 20
    total_processed = 0

    if include_variants:
        # Step 1: Sync template items first
        template_filters = {
            "has_variants": 1,
            "disabled": 0,
        }
        if synced_items:
            template_filters["name"] = ["not in", synced_items]

        template_items = frappe.get_all(
            "Item",
            filters=template_filters,
            limit=int(limit),
            pluck="name"
        )

        for item_code in template_items:
            try:
                item = frappe.get_doc("Item", item_code)
                upload_erpnext_item_to_shopware(doc=item)
                synced += 1
                total_processed += 1
            except Exception as e:
                errors += 1
                frappe.log_error(f"Failed to sync template {item_code}: {e}", "Shopware Bulk Sync Error")

            if total_processed % batch_size == 0:
                frappe.db.commit()

        # Step 2: Sync variants of template items
        # (Re-fetch synced items to include newly synced templates)
        synced_items = frappe.get_all(
            "Ecommerce Item",
            filters={"integration": MODULE_NAME},
            pluck="erpnext_item_code"
        )

        variant_filters = {
            "variant_of": ["is", "set"],
            "disabled": 0,
        }
        if synced_items:
            variant_filters["name"] = ["not in", synced_items]

        variant_items = frappe.get_all(
            "Item",
            filters=variant_filters,
            limit=int(limit),
            pluck="name"
        )

        for item_code in variant_items:
            try:
                item = frappe.get_doc("Item", item_code)
                upload_erpnext_item_to_shopware(doc=item)
                synced += 1
                total_processed += 1
            except Exception as e:
                errors += 1
                frappe.log_error(f"Failed to sync variant {item_code}: {e}", "Shopware Bulk Sync Error")

            if total_processed % batch_size == 0:
                frappe.db.commit()

    # Step 3: Sync simple items (no variants, not a variant)
    # Re-fetch synced items
    synced_items = frappe.get_all(
        "Ecommerce Item",
        filters={"integration": MODULE_NAME},
        pluck="erpnext_item_code"
    )

    simple_filters = {
        "has_variants": 0,
        "variant_of": ["is", "not set"],
        "disabled": 0,
    }
    if synced_items:
        simple_filters["name"] = ["not in", synced_items]

    simple_items = frappe.get_all(
        "Item",
        filters=simple_filters,
        limit=int(limit) - total_processed if total_processed < int(limit) else 0,
        pluck="name"
    )

    for item_code in simple_items:
        try:
            item = frappe.get_doc("Item", item_code)
            upload_erpnext_item_to_shopware(doc=item)
            synced += 1
            total_processed += 1
        except Exception as e:
            errors += 1
            frappe.log_error(f"Failed to sync {item_code}: {e}", "Shopware Bulk Sync Error")

        if total_processed % batch_size == 0:
            frappe.db.commit()

    frappe.db.commit()

    return {
        "success": True,
        "synced": synced,
        "errors": errors,
        "total": total_processed
    }


# Background Job Sync - stores status in cache
SYNC_JOB_KEY = "shopware_bulk_sync_job"


def _run_background_sync(limit: int, batch_size: int, include_variants: bool):
    """Internal function that runs in background job."""
    import time

    cache = frappe.cache()

    # Get items to sync
    synced_items = frappe.get_all(
        "Ecommerce Item",
        filters={"integration": MODULE_NAME},
        pluck="erpnext_item_code"
    )

    # Count total items to sync
    template_filters = {"has_variants": 1, "disabled": 0}
    variant_filters = {"variant_of": ["is", "set"], "disabled": 0}
    simple_filters = {"has_variants": 0, "variant_of": ["is", "not set"], "disabled": 0}

    if synced_items:
        template_filters["name"] = ["not in", synced_items]
        variant_filters["name"] = ["not in", synced_items]
        simple_filters["name"] = ["not in", synced_items]

    total_to_sync = 0
    if include_variants:
        total_to_sync += frappe.db.count("Item", template_filters)
        total_to_sync += frappe.db.count("Item", variant_filters)
    total_to_sync += frappe.db.count("Item", simple_filters)
    total_to_sync = min(total_to_sync, limit)

    # Update status
    cache.set_value(SYNC_JOB_KEY, {
        "status": "running",
        "started_at": time.time(),
        "total": total_to_sync,
        "synced": 0,
        "errors": 0,
        "current_item": None
    })

    synced = 0
    errors = 0

    try:
        # Step 1: Template items
        if include_variants:
            template_items = frappe.get_all("Item", filters=template_filters, limit=limit, pluck="name")

            for item_code in template_items:
                # Check if job was cancelled
                job_status = cache.get_value(SYNC_JOB_KEY) or {}
                if job_status.get("status") == "cancelled":
                    break

                try:
                    cache.set_value(SYNC_JOB_KEY, {**job_status, "current_item": item_code})
                    item = frappe.get_doc("Item", item_code)
                    upload_erpnext_item_to_shopware(doc=item)
                    synced += 1
                except Exception as e:
                    errors += 1
                    frappe.log_error(f"Failed to sync {item_code}: {e}", "Shopware Bulk Sync Error")

                # Update progress
                job_status = cache.get_value(SYNC_JOB_KEY) or {}
                cache.set_value(SYNC_JOB_KEY, {**job_status, "synced": synced, "errors": errors})

                if synced % batch_size == 0:
                    frappe.db.commit()

        # Check cancellation
        job_status = cache.get_value(SYNC_JOB_KEY) or {}
        if job_status.get("status") == "cancelled":
            cache.set_value(SYNC_JOB_KEY, {**job_status, "status": "cancelled", "finished_at": time.time()})
            return

        # Step 2: Variant items
        if include_variants:
            synced_items = frappe.get_all("Ecommerce Item", filters={"integration": MODULE_NAME}, pluck="erpnext_item_code")
            variant_filters = {"variant_of": ["is", "set"], "disabled": 0}
            if synced_items:
                variant_filters["name"] = ["not in", synced_items]

            variant_items = frappe.get_all("Item", filters=variant_filters, limit=limit - synced, pluck="name")

            for item_code in variant_items:
                job_status = cache.get_value(SYNC_JOB_KEY) or {}
                if job_status.get("status") == "cancelled":
                    break

                try:
                    cache.set_value(SYNC_JOB_KEY, {**job_status, "current_item": item_code})
                    item = frappe.get_doc("Item", item_code)
                    upload_erpnext_item_to_shopware(doc=item)
                    synced += 1
                except Exception as e:
                    errors += 1
                    frappe.log_error(f"Failed to sync {item_code}: {e}", "Shopware Bulk Sync Error")

                job_status = cache.get_value(SYNC_JOB_KEY) or {}
                cache.set_value(SYNC_JOB_KEY, {**job_status, "synced": synced, "errors": errors})

                if synced % batch_size == 0:
                    frappe.db.commit()

        # Check cancellation
        job_status = cache.get_value(SYNC_JOB_KEY) or {}
        if job_status.get("status") == "cancelled":
            cache.set_value(SYNC_JOB_KEY, {**job_status, "status": "cancelled", "finished_at": time.time()})
            return

        # Step 3: Simple items
        synced_items = frappe.get_all("Ecommerce Item", filters={"integration": MODULE_NAME}, pluck="erpnext_item_code")
        simple_filters = {"has_variants": 0, "variant_of": ["is", "not set"], "disabled": 0}
        if synced_items:
            simple_filters["name"] = ["not in", synced_items]

        simple_items = frappe.get_all("Item", filters=simple_filters, limit=limit - synced, pluck="name")

        for item_code in simple_items:
            job_status = cache.get_value(SYNC_JOB_KEY) or {}
            if job_status.get("status") == "cancelled":
                break

            try:
                cache.set_value(SYNC_JOB_KEY, {**job_status, "current_item": item_code})
                item = frappe.get_doc("Item", item_code)
                upload_erpnext_item_to_shopware(doc=item)
                synced += 1
            except Exception as e:
                errors += 1
                frappe.log_error(f"Failed to sync {item_code}: {e}", "Shopware Bulk Sync Error")

            job_status = cache.get_value(SYNC_JOB_KEY) or {}
            cache.set_value(SYNC_JOB_KEY, {**job_status, "synced": synced, "errors": errors})

            if synced % batch_size == 0:
                frappe.db.commit()

        frappe.db.commit()

        # Final status
        job_status = cache.get_value(SYNC_JOB_KEY) or {}
        final_status = "cancelled" if job_status.get("status") == "cancelled" else "completed"
        cache.set_value(SYNC_JOB_KEY, {
            **job_status,
            "status": final_status,
            "finished_at": time.time(),
            "synced": synced,
            "errors": errors,
            "current_item": None
        })

    except Exception as e:
        job_status = cache.get_value(SYNC_JOB_KEY) or {}
        cache.set_value(SYNC_JOB_KEY, {
            **job_status,
            "status": "error",
            "error": str(e),
            "finished_at": time.time()
        })
        raise


@frappe.whitelist()
def start_bulk_sync_job(limit: int = 30000, batch_size: int = 50, include_variants: bool = True) -> Dict[str, Any]:
    """
    Start bulk sync as a background job.

    Returns job status immediately, sync runs in background.
    """
    cache = frappe.cache()

    # Check if already running
    job_status = cache.get_value(SYNC_JOB_KEY)
    if job_status and job_status.get("status") == "running":
        return {
            "success": False,
            "message": "Sync job already running",
            "status": job_status
        }

    # Parse parameters
    limit = cint(limit) or 30000
    batch_size = cint(batch_size) or 50
    if isinstance(include_variants, str):
        include_variants = include_variants.lower() in ("true", "1", "yes")

    # Start background job
    frappe.enqueue(
        _run_background_sync,
        queue="long",
        timeout=36000,  # 10 hours
        limit=limit,
        batch_size=batch_size,
        include_variants=include_variants
    )

    return {
        "success": True,
        "message": "Sync job started",
        "status": "starting"
    }


@frappe.whitelist()
def get_bulk_sync_status() -> Dict[str, Any]:
    """Get current status of bulk sync job."""
    cache = frappe.cache()
    job_status = cache.get_value(SYNC_JOB_KEY)

    if not job_status:
        return {
            "status": "idle",
            "message": "No sync job running or completed"
        }

    return job_status


@frappe.whitelist()
def stop_bulk_sync_job() -> Dict[str, Any]:
    """Request cancellation of running sync job."""
    cache = frappe.cache()
    job_status = cache.get_value(SYNC_JOB_KEY)

    if not job_status or job_status.get("status") != "running":
        return {
            "success": False,
            "message": "No running sync job to stop"
        }

    # Set cancellation flag
    cache.set_value(SYNC_JOB_KEY, {**job_status, "status": "cancelled"})

    return {
        "success": True,
        "message": "Cancellation requested, job will stop after current item"
    }


@frappe.whitelist()
def update_all_synced_items() -> Dict[str, Any]:
    """
    Update all already synced items in Shopware.

    Returns:
        dict: Update results
    """
    setting = frappe.get_cached_doc(SETTING_DOCTYPE)

    if not setting.is_enabled():
        return {"success": False, "message": "Shopware integration is not enabled"}

    # Get all synced items
    ecom_items = frappe.get_all(
        "Ecommerce Item",
        filters={"integration": MODULE_NAME, "has_variants": 0},
        fields=["erpnext_item_code"]
    )

    updated = 0
    errors = 0

    for ecom_item in ecom_items:
        try:
            item = frappe.get_doc("Item", ecom_item.erpnext_item_code)
            upload_erpnext_item_to_shopware(doc=item)
            updated += 1
        except Exception as e:
            errors += 1
            frappe.log_error(f"Failed to update {ecom_item.erpnext_item_code}: {e}")

    return {
        "success": True,
        "updated": updated,
        "errors": errors,
        "total": len(ecom_items)
    }


@frappe.whitelist()
@temp_shopware_session
def sync_images_to_shopware(client, item_code: str) -> Dict[str, Any]:
    """
    Manually sync images for a single item to Shopware.

    Args:
        item_code: ERPNext Item code

    Returns:
        dict: Sync result
    """
    try:
        item = frappe.get_doc("Item", item_code)
        shopware_product_id = get_shopware_document_id("Item", item.name)

        if not shopware_product_id:
            return {"success": False, "message": f"Item {item_code} is not synced to Shopware"}

        success = sync_product_images_to_shopware(client, item, shopware_product_id)
        if success:
            return {"success": True, "message": f"Synced images for {item_code} to Shopware"}
        else:
            return {"success": False, "message": f"Failed to sync images for {item_code}"}
    except Exception as e:
        return {"success": False, "message": str(e)}


@frappe.whitelist()
@temp_shopware_session
def bulk_sync_images_to_shopware(client, limit: int = 100) -> Dict[str, Any]:
    """
    Bulk sync images for all synced items to Shopware.

    Args:
        limit: Maximum number of items to process

    Returns:
        dict: Sync results
    """
    setting = frappe.get_cached_doc(SETTING_DOCTYPE)

    if not setting.is_enabled():
        return {"success": False, "message": "Shopware integration is not enabled"}

    # Get all synced items
    ecom_items = frappe.get_all(
        "Ecommerce Item",
        filters={"integration": MODULE_NAME, "has_variants": 0},
        fields=["erpnext_item_code", "integration_item_code"],
        limit=int(limit)
    )

    synced = 0
    errors = 0

    for ecom_item in ecom_items:
        try:
            item = frappe.get_doc("Item", ecom_item.erpnext_item_code)
            if sync_product_images_to_shopware(client, item, ecom_item.integration_item_code):
                synced += 1
            else:
                errors += 1
        except Exception as e:
            errors += 1
            frappe.log_error(f"Failed to sync images for {ecom_item.erpnext_item_code}: {e}")

    return {
        "success": True,
        "synced": synced,
        "errors": errors,
        "total": len(ecom_items)
    }


# ============================================================================
# PRICE UPDATE FUNCTIONS
# ============================================================================


@frappe.whitelist()
@temp_shopware_session
def update_item_price_in_shopware(client, item_code: str) -> Dict[str, Any]:
    """
    Update the price for a single item in Shopware.

    This function reads the current price from ERPNext and updates
    the corresponding product in Shopware with the new price.

    Args:
        item_code: ERPNext Item code

    Returns:
        dict: Update result with success status and details
    """
    setting = frappe.get_cached_doc(SETTING_DOCTYPE)

    if not setting.is_enabled():
        return {"success": False, "message": "Shopware integration is not enabled"}

    # Get the item
    try:
        item = frappe.get_doc("Item", item_code)
    except frappe.DoesNotExistError:
        return {"success": False, "message": f"Item {item_code} not found"}

    # Check if synced
    shopware_product_id = get_shopware_document_id("Item", item.name)
    if not shopware_product_id:
        return {"success": False, "message": f"Item {item_code} is not synced to Shopware"}

    try:
        # Get price from Item Price table (more reliable than Item.standard_rate)
        setting = frappe.get_cached_doc(SETTING_DOCTYPE)
        selling_price_list = setting.get("price_list") or frappe.db.get_single_value("Selling Settings", "selling_price_list")

        # First try to get from Item Price
        net_price = frappe.db.get_value(
            "Item Price",
            {"item_code": item_code, "price_list": selling_price_list, "selling": 1},
            "price_list_rate"
        )

        # Fallback to Item.standard_rate if no Item Price found
        if not net_price:
            net_price = flt(item.get(ITEM_SELLING_RATE_FIELD) or 0)
        else:
            net_price = flt(net_price)

        if net_price <= 0:
            return {"success": False, "message": f"Item {item_code} has no valid price set in price list '{selling_price_list}'"}

        # Get tax rate from item or use default
        tax_rate = 19.0  # Default German VAT
        if hasattr(item, 'taxes') and item.taxes:
            for tax_row in item.taxes:
                if tax_row.item_tax_template:
                    template_tax_rate = frappe.db.get_value(
                        "Item Tax Template Detail",
                        {"parent": tax_row.item_tax_template},
                        "tax_rate"
                    )
                    if template_tax_rate:
                        tax_rate = flt(template_tax_rate)
                        break

        # Calculate gross price
        gross_price = round(net_price * (1 + tax_rate / 100), 2)

        # Get default currency ID (cached - eliminates 1 API call per sync)
        currency_id = get_cached_currency_id(client, "EUR")

        if not currency_id:
            return {"success": False, "message": "Could not find EUR currency in Shopware"}

        # Build price update payload
        price_payload = {
            "price": [{
                "currencyId": currency_id,
                "gross": gross_price,
                "net": net_price,
                "linked": False
            }]
        }

        # Get tax ID and update if needed
        tax_id = get_tax_id_by_rate.__wrapped__(client, tax_rate)
        if tax_id:
            price_payload["taxId"] = tax_id

        # Update price in Shopware
        client.request_patch(f"product/{shopware_product_id}", price_payload)

        create_shopware_log(
            status="Success",
            message=f"Price updated for {item_code}: Net={net_price}, Gross={gross_price}, Tax={tax_rate}%",
            request_data=price_payload,
            method="update_item_price_in_shopware"
        )

        return {
            "success": True,
            "message": f"Price updated successfully for {item_code}",
            "net_price": net_price,
            "gross_price": gross_price,
            "tax_rate": tax_rate,
            "shopware_product_id": shopware_product_id
        }

    except Exception as e:
        create_shopware_log(
            status="Error",
            message=f"Failed to update price for {item_code}: {str(e)}",
            exception=str(e),
            method="update_item_price_in_shopware",
            rollback=True
        )
        return {"success": False, "message": str(e)}


@frappe.whitelist()
@temp_shopware_session
def bulk_update_prices_in_shopware(client, limit: int = 100) -> Dict[str, Any]:
    """
    Bulk update prices for all synced items in Shopware.

    Reads current prices from ERPNext and updates the corresponding
    products in Shopware.

    Args:
        limit: Maximum number of items to process

    Returns:
        dict: Update results with counts
    """
    setting = frappe.get_cached_doc(SETTING_DOCTYPE)

    if not setting.is_enabled():
        return {"success": False, "message": "Shopware integration is not enabled"}

    # Get all synced items
    ecom_items = frappe.get_all(
        "Ecommerce Item",
        filters={"integration": MODULE_NAME, "has_variants": 0},
        fields=["erpnext_item_code", "integration_item_code"],
        limit=int(limit)
    )

    updated = 0
    skipped = 0
    errors = 0
    error_items = []

    # Get default currency ID (cached - eliminates API call)
    currency_id = get_cached_currency_id(client, "EUR")

    if not currency_id:
        return {"success": False, "message": "Could not find EUR currency in Shopware"}

    # Build batch update payload
    batch_updates = []

    for ecom_item in ecom_items:
        try:
            item = frappe.get_doc("Item", ecom_item.erpnext_item_code)

            # Get price from ERPNext (net price)
            net_price = flt(item.get(ITEM_SELLING_RATE_FIELD) or 0)

            if net_price <= 0:
                skipped += 1
                continue

            # Get tax rate from item or use default
            tax_rate = 19.0  # Default German VAT
            if hasattr(item, 'taxes') and item.taxes:
                for tax_row in item.taxes:
                    if tax_row.item_tax_template:
                        template_tax_rate = frappe.db.get_value(
                            "Item Tax Template Detail",
                            {"parent": tax_row.item_tax_template},
                            "tax_rate"
                        )
                        if template_tax_rate:
                            tax_rate = flt(template_tax_rate)
                            break

            # Calculate gross price
            gross_price = round(net_price * (1 + tax_rate / 100), 2)

            # Get tax ID
            tax_id = get_tax_id_by_rate.__wrapped__(client, tax_rate)

            # Add to batch
            update_payload = {
                "id": ecom_item.integration_item_code,
                "price": [{
                    "currencyId": currency_id,
                    "gross": gross_price,
                    "net": net_price,
                    "linked": False
                }]
            }
            if tax_id:
                update_payload["taxId"] = tax_id

            batch_updates.append(update_payload)

        except Exception as e:
            errors += 1
            error_items.append(f"{ecom_item.erpnext_item_code}: {str(e)}")

    # Send batch update using Shopware's sync API
    if batch_updates:
        try:
            sync_payload = {
                "write-product-prices": {
                    "entity": "product",
                    "action": "upsert",
                    "payload": batch_updates
                }
            }
            # Use async indexing for better performance (reduces response time from 30-60s to 1-2s)
            client.request_post("_action/sync", sync_payload, update_header_fields=HEADER_index_asynchronously)
            updated = len(batch_updates)

            create_shopware_log(
                status="Success",
                message=f"Bulk price update: {updated} items updated",
                method="bulk_update_prices_in_shopware"
            )

        except Exception as e:
            # Fall back to individual updates
            frappe.log_error(f"Bulk price update failed, falling back to individual: {e}")

            for update in batch_updates:
                try:
                    product_id = update.pop("id")
                    client.request_patch(f"product/{product_id}", update)
                    updated += 1
                except Exception as item_error:
                    errors += 1
                    error_items.append(f"Product {product_id}: {str(item_error)}")

    result = {
        "success": True,
        "updated": updated,
        "skipped": skipped,
        "errors": errors,
        "total": len(ecom_items)
    }

    if error_items:
        result["error_details"] = error_items[:10]  # First 10 errors

    return result


@frappe.whitelist()
def register_price_update_hook():
    """
    Registriert den Preis-Update-Hook in ERPNext.

    Diese Funktion registriert einen Hook, der bei Preisänderungen
    automatisch die Preise nach Shopware synchronisiert.

    Note: This is informational - the hook is automatically triggered
    by the Item on_update event in doc_events.
    """
    return {
        "info": "Price updates are automatically synced when an Item is updated.",
        "hook_location": "doc_events.Item.on_update",
        "function": "ecommerce_integrations.shopware6.bulk_sync.queue_item_for_sync",
        "manual_sync": "Use update_item_price_in_shopware() or bulk_update_prices_in_shopware() for manual sync"
    }


@frappe.whitelist()
def sync_item_group_to_shopware(item_group_name: str) -> Dict[str, Any]:
    """
    Sync an Item Group (category) to Shopware.

    This syncs the category with all its data:
    - Description
    - shopware_active (visibility toggle)
    - SEO fields
    - FAQ fields
    - Category image

    Args:
        item_group_name: Name of the ERPNext Item Group

    Returns:
        dict: Sync result with success status and message
    """
    try:
        setting = frappe.get_cached_doc(SETTING_DOCTYPE)
        if not setting.is_enabled():
            return {"success": False, "message": "Shopware integration is not enabled"}

        # Sync the category hierarchy (this will create/update the category)
        category_id = sync_category_hierarchy(item_group_name=item_group_name)

        if category_id:
            create_shopware_log(
                status="Success",
                message=f"Synced Item Group to Shopware: {item_group_name} -> {category_id}",
                method="sync_item_group_to_shopware"
            )
            return {
                "success": True,
                "message": f"Synced {item_group_name} to Shopware",
                "shopware_category_id": category_id
            }
        else:
            return {
                "success": False,
                "message": f"Could not sync {item_group_name} - category may be a root category or sync failed"
            }

    except Exception as e:
        frappe.log_error(f"Failed to sync Item Group {item_group_name}: {e}", "Shopware Item Group Sync")
        return {"success": False, "message": str(e)}


# =============================================================================
# CATEGORY-ONLY SYNC (ALL CATEGORIES)
# =============================================================================

@frappe.whitelist()
@temp_shopware_session
def sync_all_categories_to_shopware(
    client,
    root_category: str = "Produkte",
    skip_root: bool = False,
    sync_empty_categories: bool = True,
    dry_run: bool = False
) -> Dict[str, Any]:
    """
    Sync ALL Item Groups under a root category to Shopware.

    Processes in tree order (parent before children) using lft ordering.
    Creates/updates categories even if they have no products assigned.

    Args:
        client: Shopware API client (injected by decorator)
        root_category: Root category to start from (default: "Produkte")
        skip_root: If True, don't sync the root category itself
        sync_empty_categories: If True, sync categories even without products
        dry_run: If True, only report what would be synced

    Returns:
        dict: Sync results with statistics
    """
    # Convert string parameters from frontend
    if isinstance(skip_root, str):
        skip_root = skip_root.lower() in ("true", "1", "yes")
    if isinstance(sync_empty_categories, str):
        sync_empty_categories = sync_empty_categories.lower() in ("true", "1", "yes")
    if isinstance(dry_run, str):
        dry_run = dry_run.lower() in ("true", "1", "yes")

    setting = frappe.get_cached_doc(SETTING_DOCTYPE)
    if not setting.is_enabled():
        return {"success": False, "message": "Shopware integration is not enabled"}

    # Get root category's lft/rgt values for nested set query
    root_info = frappe.db.get_value(
        "Item Group",
        root_category,
        ["lft", "rgt", "name"],
        as_dict=True
    )

    if not root_info:
        return {"success": False, "message": f"Root category '{root_category}' not found"}

    # Build filters for all descendants
    filters = [
        ["lft", ">=", root_info.lft],
        ["rgt", "<=", root_info.rgt]
    ]

    if skip_root:
        filters.append(["name", "!=", root_category])

    # Get all categories in tree order (lft asc = parent before children)
    categories = frappe.get_all(
        "Item Group",
        filters=filters,
        fields=["name", "parent_item_group", "is_group"],
        order_by="lft asc"
    )

    # Optionally filter out empty categories (categories without products)
    if not sync_empty_categories:
        categories_with_products = set(frappe.get_all(
            "Item",
            filters={"disabled": 0},
            pluck="item_group"
        ))
        categories = [c for c in categories if c.name in categories_with_products]

    # Initialize statistics
    stats = {
        "synced": 0,
        "skipped": 0,
        "errors": [],
        "total": len(categories)
    }

    synced_categories = []

    frappe.logger().info(f"Sync All Categories: Processing {len(categories)} categories from root '{root_category}'")

    for cat in categories:
        if dry_run:
            stats["synced"] += 1
            synced_categories.append(cat.name)
            continue

        try:
            # Sync the category hierarchy (creates/updates the category)
            category_id = sync_category_hierarchy.__wrapped__(
                client=client,
                item_group_name=cat.name
            )

            if category_id:
                stats["synced"] += 1
                synced_categories.append(cat.name)
            else:
                stats["skipped"] += 1

        except Exception as e:
            stats["errors"].append({"category": cat.name, "error": str(e)[:200]})
            frappe.log_error(f"Failed to sync category {cat.name}: {e}", "Shopware Category Sync")

        # Commit periodically to avoid long transactions
        if stats["synced"] % 20 == 0:
            frappe.db.commit()

    frappe.db.commit()

    # Create log entry
    create_shopware_log(
        status="Success" if not stats["errors"] else "Partial",
        message=f"Category sync: {stats['synced']}/{stats['total']} synced, {len(stats['errors'])} errors",
        request_data={
            "root_category": root_category,
            "skip_root": skip_root,
            "sync_empty_categories": sync_empty_categories,
            "dry_run": dry_run
        },
        method="sync_all_categories_to_shopware"
    )

    return {
        "success": True,
        "dry_run": dry_run,
        "statistics": stats,
        "synced_categories": synced_categories[:50] if dry_run else [],  # Only include list in dry run
        "message": f"Synced {stats['synced']}/{stats['total']} categories" + (" (DRY RUN)" if dry_run else "")
    }


@frappe.whitelist()
@temp_shopware_session
def cleanup_orphaned_shopware_categories(
    client,
    root_category: str = "Produkte",
    dry_run: bool = True
) -> Dict[str, Any]:
    """
    Delete categories in Shopware that no longer exist in ERPNext.

    This function compares categories in Shopware with ERPNext Item Groups
    and removes any Shopware categories that don't have a matching Item Group.

    IMPORTANT: Only deletes categories under the specified root, not the entire Shopware tree.

    Args:
        client: Shopware API client (injected by decorator)
        root_category: ERPNext root category to compare against (default: "Produkte")
        dry_run: If True, only report what would be deleted (default: True for safety)

    Returns:
        dict: Cleanup results with statistics
    """
    # Convert string parameters
    if isinstance(dry_run, str):
        dry_run = dry_run.lower() in ("true", "1", "yes")

    setting = frappe.get_cached_doc(SETTING_DOCTYPE)
    if not setting.is_enabled():
        return {"success": False, "message": "Shopware integration is not enabled"}

    # Get all ERPNext Item Groups under the root category
    root_info = frappe.db.get_value(
        "Item Group",
        root_category,
        ["lft", "rgt"],
        as_dict=True
    )

    if not root_info:
        return {"success": False, "message": f"Root category '{root_category}' not found in ERPNext"}

    # Get all ERPNext category names under root
    erpnext_categories = set(frappe.get_all(
        "Item Group",
        filters=[
            ["lft", ">=", root_info.lft],
            ["rgt", "<=", root_info.rgt]
        ],
        pluck="name"
    ))

    # Categories to skip (ERPNext root categories that shouldn't be in Shopware)
    skip_categories = {"All Item Groups", "Alle Artikelgruppen"}
    erpnext_categories = erpnext_categories - skip_categories

    frappe.logger().info(f"Cleanup: Found {len(erpnext_categories)} ERPNext categories under '{root_category}'")

    # Get all Shopware categories
    try:
        # Fetch all categories from Shopware (paginated)
        all_shopware_categories = []
        page = 1
        limit = 100

        while True:
            response = client.request_post(
                "search/category",
                {
                    "limit": limit,
                    "page": page,
                    "filter": [
                        # Exclude root category (parentId = null)
                        {"type": "not", "queries": [{"type": "equals", "field": "parentId", "value": None}]}
                    ]
                }
            )
            categories = response.get("data", [])
            if not categories:
                break

            all_shopware_categories.extend(categories)
            if len(categories) < limit:
                break
            page += 1

    except Exception as e:
        return {"success": False, "message": f"Failed to fetch Shopware categories: {str(e)}"}

    frappe.logger().info(f"Cleanup: Found {len(all_shopware_categories)} categories in Shopware")

    # Find orphaned categories (in Shopware but not in ERPNext)
    orphaned_categories = []
    for cat in all_shopware_categories:
        cat_name = cat.get("name", "")
        cat_id = cat.get("id", "")

        # Check if this category exists in ERPNext
        if cat_name and cat_name not in erpnext_categories:
            orphaned_categories.append({
                "id": cat_id,
                "name": cat_name,
                "parent_id": cat.get("parentId")
            })

    stats = {
        "erpnext_count": len(erpnext_categories),
        "shopware_count": len(all_shopware_categories),
        "orphaned_count": len(orphaned_categories),
        "deleted": 0,
        "failed": 0,
        "errors": []
    }

    if not orphaned_categories:
        return {
            "success": True,
            "dry_run": dry_run,
            "statistics": stats,
            "message": "No orphaned categories found - Shopware is in sync with ERPNext"
        }

    # Sort by depth (delete children before parents) - categories with no children first
    # We'll delete in reverse order of creation to avoid parent-child conflicts
    orphaned_categories_to_delete = []

    if not dry_run:
        # Build parent-child relationships to delete in correct order
        # Delete leaf categories first (no children)
        orphaned_ids = {cat["id"] for cat in orphaned_categories}

        # Find categories that have no children in the orphaned set
        for cat in orphaned_categories:
            has_orphaned_children = any(
                other["parent_id"] == cat["id"]
                for other in orphaned_categories
            )
            cat["has_children"] = has_orphaned_children

        # Sort: categories without children first
        orphaned_categories.sort(key=lambda x: x.get("has_children", False))

        for cat in orphaned_categories:
            try:
                client.request_delete(f"category/{cat['id']}")
                stats["deleted"] += 1
                orphaned_categories_to_delete.append(cat["name"])
                frappe.logger().info(f"Deleted orphaned category: {cat['name']} ({cat['id']})")
            except Exception as e:
                stats["failed"] += 1
                stats["errors"].append({"category": cat["name"], "error": str(e)[:100]})
                frappe.log_error(f"Failed to delete category {cat['name']}: {e}", "Shopware Category Cleanup")

    # Create log entry
    create_shopware_log(
        status="Success" if stats["failed"] == 0 else "Partial",
        message=f"Category cleanup: {stats['deleted']} deleted, {stats['failed']} failed" if not dry_run
                else f"Category cleanup (DRY RUN): {len(orphaned_categories)} would be deleted",
        request_data={
            "root_category": root_category,
            "dry_run": dry_run,
            "orphaned_categories": [c["name"] for c in orphaned_categories[:20]]
        },
        method="cleanup_orphaned_shopware_categories"
    )

    return {
        "success": True,
        "dry_run": dry_run,
        "statistics": stats,
        "orphaned_categories": [c["name"] for c in orphaned_categories[:50]],
        "deleted_categories": orphaned_categories_to_delete[:50] if not dry_run else [],
        "message": (
            f"Found {len(orphaned_categories)} orphaned categories. "
            + (f"Deleted {stats['deleted']}, Failed {stats['failed']}" if not dry_run
               else "Run without dry_run to delete them.")
        )
    }


@frappe.whitelist()
@temp_shopware_session
def analyze_shopware_categories(client) -> Dict[str, Any]:
    """
    Analyze the Shopware category tree structure.

    Returns a detailed view of all categories in Shopware, organized by their tree structure.
    Useful for understanding the category hierarchy before running cleanup operations.

    Returns:
        dict: Category tree analysis with statistics
    """
    setting = frappe.get_cached_doc(SETTING_DOCTYPE)
    if not setting.is_enabled():
        return {"success": False, "message": "Shopware integration is not enabled"}

    # Fetch all categories from Shopware
    all_categories = []
    page = 1
    limit = 100

    while True:
        response = client.request_post(
            "search/category",
            {
                "limit": limit,
                "page": page,
                "includes": {
                    "category": ["id", "parentId", "name", "translated", "level", "childCount", "active", "path"]
                }
            }
        )
        categories = response.get("data", [])
        if not categories:
            break
        all_categories.extend(categories)
        if len(categories) < limit:
            break
        page += 1

    # Build lookup and tree
    cats_by_id = {c["id"]: c for c in all_categories}

    def get_name(c):
        return c.get("name") or c.get("translated", {}).get("name") or "(unnamed)"

    # Find root categories (no parent or parent not found)
    roots = []
    for c in all_categories:
        parent_id = c.get("parentId")
        if not parent_id or parent_id not in cats_by_id:
            roots.append(c)

    def build_tree(cat, depth=0):
        children = [c for c in all_categories if c.get("parentId") == cat["id"]]
        children.sort(key=lambda x: get_name(x))
        return {
            "id": cat["id"],
            "name": get_name(cat),
            "active": cat.get("active", True),
            "child_count": cat.get("childCount", 0),
            "level": cat.get("level", depth),
            "children": [build_tree(child, depth + 1) for child in children] if depth < 3 else []
        }

    # Build tree for each root
    tree = []
    for root in sorted(roots, key=get_name):
        tree.append(build_tree(root))

    # Get ERPNext categories for comparison
    erpnext_names = set(frappe.get_all("Item Group", pluck="name"))

    # Categorize Shopware categories
    only_in_shopware = []
    in_both = []
    for cat in all_categories:
        name = get_name(cat)
        if name in erpnext_names:
            in_both.append(name)
        else:
            only_in_shopware.append(name)

    return {
        "success": True,
        "statistics": {
            "total_shopware": len(all_categories),
            "total_erpnext": len(erpnext_names),
            "in_both": len(in_both),
            "only_in_shopware": len(only_in_shopware),
            "root_categories": len(roots)
        },
        "root_categories": [get_name(r) for r in roots],
        "only_in_shopware": sorted(only_in_shopware),
        "tree": tree
    }


@frappe.whitelist()
@temp_shopware_session
def cleanup_orphaned_shopware_categories_v2(
    client,
    root_category: str = "Produkte",
    shopware_root_category: str = None,
    exclude_parent_categories: str = None,
    dry_run: bool = True
) -> Dict[str, Any]:
    """
    Delete categories in Shopware that no longer exist in ERPNext.

    Enhanced version with support for excluding entire subtrees from cleanup.

    Args:
        client: Shopware API client (injected by decorator)
        root_category: ERPNext root category to compare against (default: "Produkte")
        shopware_root_category: Shopware root category name to search under (default: same as root_category)
        exclude_parent_categories: Comma-separated list of Shopware parent category names to exclude
                                   (e.g., "Footer,Service,Rechtliches" - their children won't be deleted)
        dry_run: If True, only report what would be deleted (default: True for safety)

    Returns:
        dict: Cleanup results with statistics
    """
    # Convert string parameters
    if isinstance(dry_run, str):
        dry_run = dry_run.lower() in ("true", "1", "yes")

    setting = frappe.get_cached_doc(SETTING_DOCTYPE)
    if not setting.is_enabled():
        return {"success": False, "message": "Shopware integration is not enabled"}

    # Parse excluded parent categories
    excluded_parents = set()
    if exclude_parent_categories:
        excluded_parents = {name.strip() for name in exclude_parent_categories.split(",") if name.strip()}
        frappe.logger().info(f"Cleanup: Excluding parent categories: {excluded_parents}")

    if shopware_root_category is None:
        shopware_root_category = root_category

    # Get all ERPNext Item Groups under the root category
    root_info = frappe.db.get_value(
        "Item Group",
        root_category,
        ["lft", "rgt"],
        as_dict=True
    )

    if not root_info:
        return {"success": False, "message": f"Root category '{root_category}' not found in ERPNext"}

    # Get all ERPNext category names under root
    erpnext_categories = set(frappe.get_all(
        "Item Group",
        filters=[
            ["lft", ">=", root_info.lft],
            ["rgt", "<=", root_info.rgt]
        ],
        pluck="name"
    ))

    # Categories to skip (ERPNext root categories)
    skip_categories = {"All Item Groups", "Alle Artikelgruppen"}
    erpnext_categories = erpnext_categories - skip_categories

    frappe.logger().info(f"Cleanup v2: Found {len(erpnext_categories)} ERPNext categories under '{root_category}'")

    # Fetch all Shopware categories
    all_shopware_categories = []
    page = 1
    limit = 100

    while True:
        response = client.request_post(
            "search/category",
            {
                "limit": limit,
                "page": page,
                "includes": {
                    "category": ["id", "parentId", "name", "translated", "level", "path"]
                }
            }
        )
        categories = response.get("data", [])
        if not categories:
            break
        all_shopware_categories.extend(categories)
        if len(categories) < limit:
            break
        page += 1

    frappe.logger().info(f"Cleanup v2: Found {len(all_shopware_categories)} categories in Shopware")

    # Build category lookup
    cats_by_id = {c["id"]: c for c in all_shopware_categories}

    def get_name(c):
        return c.get("name") or c.get("translated", {}).get("name") or ""

    # Find the Shopware root category (e.g., "Produkte")
    shopware_root = None
    for cat in all_shopware_categories:
        if get_name(cat) == shopware_root_category:
            shopware_root = cat
            break

    # Get IDs of excluded parent categories and all their descendants
    excluded_category_ids = set()
    for cat in all_shopware_categories:
        cat_name = get_name(cat)
        if cat_name in excluded_parents:
            # Add this category and find all its descendants
            excluded_category_ids.add(cat["id"])
            # Find all descendants by checking path
            for other in all_shopware_categories:
                if other.get("path") and cat["id"] in (other.get("path") or ""):
                    excluded_category_ids.add(other["id"])

    frappe.logger().info(f"Cleanup v2: Excluding {len(excluded_category_ids)} categories (and descendants) from cleanup")

    # Find orphaned categories (in Shopware but not in ERPNext)
    # Only consider categories under the Shopware root and not in excluded trees
    orphaned_categories = []
    categories_under_root = []

    for cat in all_shopware_categories:
        cat_name = get_name(cat)
        cat_id = cat.get("id", "")
        cat_path = cat.get("path") or ""

        # Skip if this category is in an excluded tree
        if cat_id in excluded_category_ids:
            continue

        # If we have a root, only consider categories under it
        if shopware_root:
            # Check if category is under the root by examining path
            if shopware_root["id"] not in cat_path and cat_id != shopware_root["id"]:
                continue

        categories_under_root.append(cat)

        # Check if this category exists in ERPNext
        if cat_name and cat_name not in erpnext_categories:
            orphaned_categories.append({
                "id": cat_id,
                "name": cat_name,
                "parent_id": cat.get("parentId")
            })

    stats = {
        "erpnext_count": len(erpnext_categories),
        "shopware_count": len(all_shopware_categories),
        "shopware_under_root": len(categories_under_root),
        "excluded_count": len(excluded_category_ids),
        "orphaned_count": len(orphaned_categories),
        "deleted": 0,
        "failed": 0,
        "errors": []
    }

    if not orphaned_categories:
        return {
            "success": True,
            "dry_run": dry_run,
            "statistics": stats,
            "excluded_parents": list(excluded_parents),
            "message": "No orphaned categories found - Shopware is in sync with ERPNext"
        }

    deleted_categories = []

    if not dry_run:
        # Build parent-child relationships for correct deletion order
        orphaned_ids = {cat["id"] for cat in orphaned_categories}

        # Mark categories that have children in the orphaned set
        for cat in orphaned_categories:
            has_orphaned_children = any(
                other["parent_id"] == cat["id"]
                for other in orphaned_categories
            )
            cat["has_children"] = has_orphaned_children

        # Sort: categories without children first (leaves first)
        orphaned_categories.sort(key=lambda x: x.get("has_children", False))

        for cat in orphaned_categories:
            try:
                client.request_delete(f"category/{cat['id']}")
                stats["deleted"] += 1
                deleted_categories.append(cat["name"])
                frappe.logger().info(f"Deleted orphaned category: {cat['name']} ({cat['id']})")
            except Exception as e:
                stats["failed"] += 1
                stats["errors"].append({"category": cat["name"], "error": str(e)[:100]})
                frappe.log_error(f"Failed to delete category {cat['name']}: {e}", "Shopware Category Cleanup")

    # Create log entry
    create_shopware_log(
        status="Success" if stats["failed"] == 0 else "Partial",
        message=f"Category cleanup v2: {stats['deleted']} deleted, {stats['failed']} failed" if not dry_run
                else f"Category cleanup v2 (DRY RUN): {len(orphaned_categories)} would be deleted",
        request_data={
            "root_category": root_category,
            "shopware_root_category": shopware_root_category,
            "excluded_parents": list(excluded_parents),
            "dry_run": dry_run,
            "orphaned_categories": [c["name"] for c in orphaned_categories[:20]]
        },
        method="cleanup_orphaned_shopware_categories_v2"
    )

    return {
        "success": True,
        "dry_run": dry_run,
        "statistics": stats,
        "excluded_parents": list(excluded_parents),
        "orphaned_categories": [c["name"] for c in orphaned_categories[:50]],
        "deleted_categories": deleted_categories[:50] if not dry_run else [],
        "message": (
            f"Found {len(orphaned_categories)} orphaned categories (excluded {len(excluded_category_ids)} in protected trees). "
            + (f"Deleted {stats['deleted']}, Failed {stats['failed']}" if not dry_run
               else "Run without dry_run to delete them.")
        )
    }


@frappe.whitelist()
def full_reconciliation(
    limit: int = 500,
    dry_run: bool = False,
    sync_images: bool = False,
    include_unlinked: bool = False,
    category_root: str = "Produkte",
    skip_root_category: bool = False,
    sync_empty_categories: bool = True,
    cleanup_orphaned_categories: bool = False
) -> Dict[str, Any]:
    """
    Full reconciliation: Categories first, then Products, optionally cleanup.

    This is the recommended function for a complete ERPNext to Shopware sync.
    It ensures all categories exist before syncing products and can optionally
    remove categories from Shopware that no longer exist in ERPNext.

    Args:
        limit: Maximum products to process
        dry_run: If True, only report differences without syncing
        sync_images: If True, also sync images for changed items
        include_unlinked: If True, also sync products not yet in Shopware
        category_root: Root category for category sync (default: "Produkte")
        skip_root_category: If True, skip the root category itself
        sync_empty_categories: If True, sync categories even without products
        cleanup_orphaned_categories: If True, delete Shopware categories not in ERPNext

    Returns:
        dict: Combined results from category, product sync, and cleanup
    """
    # Convert string parameters from frontend
    if isinstance(dry_run, str):
        dry_run = dry_run.lower() in ("true", "1", "yes")
    if isinstance(sync_images, str):
        sync_images = sync_images.lower() in ("true", "1", "yes")
    if isinstance(include_unlinked, str):
        include_unlinked = include_unlinked.lower() in ("true", "1", "yes")
    if isinstance(skip_root_category, str):
        skip_root_category = skip_root_category.lower() in ("true", "1", "yes")
    if isinstance(sync_empty_categories, str):
        sync_empty_categories = sync_empty_categories.lower() in ("true", "1", "yes")
    if isinstance(cleanup_orphaned_categories, str):
        cleanup_orphaned_categories = cleanup_orphaned_categories.lower() in ("true", "1", "yes")

    results = {
        "success": True,
        "dry_run": dry_run,
        "category_sync": None,
        "product_sync": None,
        "category_cleanup": None
    }

    frappe.logger().info(f"Full Reconciliation: Starting (categories: {category_root}, products: {limit}, cleanup: {cleanup_orphaned_categories})")

    # Phase 1: Sync ALL categories first
    frappe.publish_realtime(
        "msgprint",
        {"message": "Phase 1/3: Syncing categories...", "indicator": "blue"},
        user=frappe.session.user
    )

    category_result = sync_all_categories_to_shopware(
        root_category=category_root,
        skip_root=skip_root_category,
        sync_empty_categories=sync_empty_categories,
        dry_run=dry_run
    )
    results["category_sync"] = category_result

    # Phase 2: Sync products (reuse existing reconciliation)
    frappe.publish_realtime(
        "msgprint",
        {"message": "Phase 2/3: Syncing products...", "indicator": "blue"},
        user=frappe.session.user
    )

    product_result = reconcile_erpnext_with_shopware(
        limit=int(limit),
        dry_run=dry_run,
        sync_images=sync_images,
        include_unlinked=include_unlinked,
        compare_categories=True
    )
    results["product_sync"] = product_result

    # Phase 3: Cleanup orphaned categories (if enabled)
    if cleanup_orphaned_categories:
        frappe.publish_realtime(
            "msgprint",
            {"message": "Phase 3/3: Cleaning up orphaned categories...", "indicator": "blue"},
            user=frappe.session.user
        )

        cleanup_result = cleanup_orphaned_shopware_categories(
            root_category=category_root,
            dry_run=dry_run
        )
        results["category_cleanup"] = cleanup_result

    # Combine statistics for summary
    cat_stats = category_result.get("statistics", {})
    prod_stats = product_result.get("statistics", {})

    message_parts = [
        f"Categories: {cat_stats.get('synced', 0)}/{cat_stats.get('total', 0)} synced",
        f"Products: {prod_stats.get('synced', 0)}/{prod_stats.get('total_checked', 0)} synced"
    ]

    if cleanup_orphaned_categories and results.get("category_cleanup"):
        cleanup_stats = results["category_cleanup"].get("statistics", {})
        message_parts.append(f"Cleanup: {cleanup_stats.get('deleted', 0)} deleted")

    results["message"] = ". ".join(message_parts) + "."

    if dry_run:
        results["message"] += " (DRY RUN - no changes made)"

    # Create combined log entry
    create_shopware_log(
        status="Success",
        message=results["message"],
        request_data={
            "limit": limit,
            "dry_run": dry_run,
            "category_root": category_root,
            "skip_root_category": skip_root_category,
            "cleanup_orphaned_categories": cleanup_orphaned_categories
        },
        method="full_reconciliation"
    )

    return results


@frappe.whitelist()
def enqueue_full_reconciliation_with_categories(
    limit: int = 500,
    dry_run: bool = False,
    sync_images: bool = False,
    include_unlinked: bool = False,
    category_root: str = None,
    skip_root_category: bool = None,
    sync_empty_categories: bool = None,
    cleanup_orphaned_categories: bool = False
) -> Dict[str, Any]:
    """
    Enqueue full reconciliation (categories + products + cleanup) as background job.

    Use this for large reconciliations that would timeout in a web request.

    Args:
        limit: Maximum products to process
        dry_run: If True, only report differences without syncing
        sync_images: If True, also sync images for changed items
        include_unlinked: If True, also sync products not yet in Shopware
        cleanup_orphaned_categories: If True, delete categories in Shopware not in ERPNext
        category_root: Root category (uses setting default if not provided)
        skip_root_category: Skip root category (uses setting default if not provided)
        sync_empty_categories: Sync empty categories (uses setting default if not provided)

    Returns:
        dict: Job enqueue status
    """
    # Convert string parameters
    if isinstance(dry_run, str):
        dry_run = dry_run.lower() in ("true", "1", "yes")
    if isinstance(sync_images, str):
        sync_images = sync_images.lower() in ("true", "1", "yes")
    if isinstance(include_unlinked, str):
        include_unlinked = include_unlinked.lower() in ("true", "1", "yes")
    if isinstance(cleanup_orphaned_categories, str):
        cleanup_orphaned_categories = cleanup_orphaned_categories.lower() in ("true", "1", "yes")

    # Load settings for defaults if not provided
    setting = frappe.get_cached_doc(SETTING_DOCTYPE)

    if category_root is None:
        category_root = getattr(setting, 'category_sync_root', 'Produkte') or 'Produkte'
    if skip_root_category is None:
        skip_root_category = getattr(setting, 'skip_root_category', False)
    if sync_empty_categories is None:
        sync_empty_categories = getattr(setting, 'sync_empty_categories', True)

    # Convert to bool if string
    if isinstance(skip_root_category, str):
        skip_root_category = skip_root_category.lower() in ("true", "1", "yes")
    if isinstance(sync_empty_categories, str):
        sync_empty_categories = sync_empty_categories.lower() in ("true", "1", "yes")

    frappe.enqueue(
        "ecommerce_integrations.shopware6.product_export.full_reconciliation",
        queue="long",
        timeout=3600,  # 1 hour timeout
        job_name=f"shopware6_full_reconciliation_{frappe.utils.now()}",
        limit=int(limit),
        dry_run=dry_run,
        sync_images=sync_images,
        include_unlinked=include_unlinked,
        category_root=category_root,
        skip_root_category=skip_root_category,
        sync_empty_categories=sync_empty_categories,
        cleanup_orphaned_categories=cleanup_orphaned_categories
    )

    cleanup_info = " + cleanup" if cleanup_orphaned_categories else ""
    return {
        "success": True,
        "message": f"Full reconciliation job enqueued (categories from '{category_root}' + up to {limit} products{cleanup_info})",
        "job_queue": "long",
        "dry_run": dry_run
    }


# =============================================================================
# FULL RECONCILIATION SYNC
# =============================================================================

def get_shopware_products_batch(client, product_ids: List[str], include_categories: bool = True) -> Dict[str, Dict[str, Any]]:
    """
    Fetch multiple products from Shopware in a single API call using search.

    This is much faster than fetching products one by one.

    Args:
        client: Shopware API client
        product_ids: List of Shopware product UUIDs
        include_categories: If True, include category associations

    Returns:
        Dict mapping product ID to product data
    """
    if not product_ids:
        return {}

    try:
        from lib_shopware6_api_base import Criteria, EqualsFilter, MultiFilter

        # Build search criteria for multiple IDs
        search_payload = {
            "limit": len(product_ids),
            "ids": product_ids,
        }

        if include_categories:
            search_payload["associations"] = {"categories": {}}

        response = client.request_post("search/product", search_payload)

        # Build result dict mapping ID to product data
        result = {}
        for product in response.get("data", []):
            product_id = product.get("id")
            if product_id:
                result[product_id] = product

        return result

    except Exception as e:
        frappe.log_error(f"Batch product fetch failed: {e}", "Shopware Reconciliation")
        return {}


def get_shopware_product_data(client, shopware_product_id: str, include_categories: bool = True) -> Optional[Dict[str, Any]]:
    """
    Fetch product data from Shopware for comparison.

    Args:
        client: Shopware API client
        shopware_product_id: Shopware product UUID
        include_categories: If True, include category associations (default True)

    Returns:
        Product data dict or None if not found
    """
    try:
        # Include categories association if requested
        if include_categories:
            response = client.request_get(f"product/{shopware_product_id}?associations[categories][]")
        else:
            response = client.request_get(f"product/{shopware_product_id}")
        return response.get("data", {})
    except BaseException:
        return None


def compare_item_with_shopware(erpnext_item, shopware_data: Dict[str, Any], compare_categories: bool = True) -> Dict[str, Any]:
    """
    Compare ERPNext Item with Shopware product data.

    Returns a dict with:
    - needs_sync: bool - True if differences found
    - differences: list - List of field names that differ
    - details: dict - Details of each difference

    Args:
        erpnext_item: ERPNext Item document
        shopware_data: Shopware product data from API
        compare_categories: If True, also compare categories (default True)

    Returns:
        Comparison result dict
    """
    differences = []
    details = {}

    # Compare name
    erpnext_name = erpnext_item.item_name
    shopware_name = shopware_data.get("name", "")
    if erpnext_name != shopware_name:
        differences.append("name")
        details["name"] = {"erpnext": erpnext_name, "shopware": shopware_name}

    # Compare description
    erpnext_desc = (
        getattr(erpnext_item, 'web_long_description', None) or
        erpnext_item.description or
        erpnext_item.item_name
    )
    shopware_desc = shopware_data.get("description", "")
    # Only compare if both have content (Shopware may have HTML formatting)
    if erpnext_desc and shopware_desc:
        # Simple comparison - strip HTML for basic check
        erpnext_desc_clean = erpnext_desc.strip() if erpnext_desc else ""
        shopware_desc_clean = shopware_desc.strip() if shopware_desc else ""
        if erpnext_desc_clean != shopware_desc_clean:
            differences.append("description")
            details["description"] = {
                "erpnext": erpnext_desc_clean[:100] + "..." if len(erpnext_desc_clean) > 100 else erpnext_desc_clean,
                "shopware": shopware_desc_clean[:100] + "..." if len(shopware_desc_clean) > 100 else shopware_desc_clean
            }

    # Compare active status
    erpnext_active = not erpnext_item.disabled
    shopware_active = shopware_data.get("active", False)
    if erpnext_active != shopware_active:
        differences.append("active")
        details["active"] = {"erpnext": erpnext_active, "shopware": shopware_active}

    # Compare price (with tolerance for rounding)
    erpnext_price = flt(erpnext_item.get(ITEM_SELLING_RATE_FIELD) or 0)
    shopware_prices = shopware_data.get("price", [])
    if shopware_prices and erpnext_price > 0:
        shopware_net = flt(shopware_prices[0].get("net", 0))
        # Allow 1 cent tolerance for rounding differences
        if abs(erpnext_price - shopware_net) > 0.01:
            differences.append("price")
            details["price"] = {"erpnext_net": erpnext_price, "shopware_net": shopware_net}

    # Compare weight
    erpnext_weight = flt(erpnext_item.weight_per_unit or 0)
    shopware_weight = flt(shopware_data.get("weight", 0))
    if abs(erpnext_weight - shopware_weight) > 0.001:
        differences.append("weight")
        details["weight"] = {"erpnext": erpnext_weight, "shopware": shopware_weight}

    # Compare product number (item_code)
    if erpnext_item.item_code != shopware_data.get("productNumber", ""):
        differences.append("productNumber")
        details["productNumber"] = {
            "erpnext": erpnext_item.item_code,
            "shopware": shopware_data.get("productNumber", "")
        }

    # Compare categories (if enabled and category data is included)
    if compare_categories:
        shopware_categories = shopware_data.get("categories", [])
        # Get expected categories from ERPNext
        erpnext_categories = get_all_item_categories(erpnext_item.item_code)

        # Get Shopware category names from the cached mapping
        shopware_category_names = []
        for cat in shopware_categories:
            cat_name = cat.get("name") or cat.get("translated", {}).get("name", "")
            if cat_name:
                shopware_category_names.append(cat_name)

        # Sort both for comparison
        erpnext_categories_sorted = sorted(erpnext_categories) if erpnext_categories else []
        shopware_category_names_sorted = sorted(shopware_category_names) if shopware_category_names else []

        # Check if categories differ
        if erpnext_categories_sorted != shopware_category_names_sorted:
            differences.append("categories")
            details["categories"] = {
                "erpnext": erpnext_categories_sorted,
                "shopware": shopware_category_names_sorted
            }

    return {
        "needs_sync": len(differences) > 0,
        "differences": differences,
        "details": details
    }


@frappe.whitelist()
@temp_shopware_session
def reconcile_erpnext_with_shopware(
    client,
    limit: int = 100,
    dry_run: bool = False,
    sync_images: bool = False,
    include_unlinked: bool = False,
    compare_categories: bool = True
) -> Dict[str, Any]:
    """
    Full reconciliation: Compare all ERPNext items with Shopware and sync differences.

    This function:
    1. Loads all synced items from ERPNext (via Ecommerce Item)
    2. Fetches corresponding product data from Shopware
    3. Compares fields: name, description, price, active status, weight, categories
    4. Syncs items where differences are found
    5. Optionally syncs items not yet linked to Shopware

    Args:
        client: Shopware API client (injected by decorator)
        limit: Maximum number of items to process (default 100)
        dry_run: If True, only report differences without syncing (default False)
        sync_images: If True, also sync images for changed items (default False)
        include_unlinked: If True, also sync items not yet in Shopware (default False)
        compare_categories: If True, also compare and sync categories (default True)

    Returns:
        dict: Reconciliation results with statistics and details
    """
    setting = frappe.get_cached_doc(SETTING_DOCTYPE)

    if not setting.is_enabled():
        return {"success": False, "message": "Shopware integration is not enabled"}

    # Statistics
    stats = {
        "total_checked": 0,
        "in_sync": 0,
        "out_of_sync": 0,
        "synced": 0,
        "sync_failed": 0,
        "not_in_shopware": 0,
        "newly_synced": 0,
        "errors": []
    }

    # Details for report
    out_of_sync_items = []
    synced_items = []

    # Get all linked items
    ecom_items = frappe.get_all(
        "Ecommerce Item",
        filters={"integration": MODULE_NAME},
        fields=["erpnext_item_code", "integration_item_code", "has_variants"],
        limit=int(limit)
    )

    frappe.logger().info(f"Reconciliation: Checking {len(ecom_items)} linked items")

    # Process in batches for better performance
    batch_size = 50  # Fetch 50 products at a time from Shopware

    for batch_start in range(0, len(ecom_items), batch_size):
        batch = ecom_items[batch_start:batch_start + batch_size]

        # Collect all Shopware IDs for this batch
        shopware_ids = [item.integration_item_code for item in batch]

        # Batch fetch all products from Shopware (single API call per batch)
        shopware_products = get_shopware_products_batch(client, shopware_ids, include_categories=compare_categories)

        # Process each item in the batch
        for ecom_item in batch:
            stats["total_checked"] += 1
            item_code = ecom_item.erpnext_item_code
            shopware_id = ecom_item.integration_item_code

            try:
                # Load ERPNext item
                erpnext_item = frappe.get_doc("Item", item_code)

                # Get Shopware data from batch result
                shopware_data = shopware_products.get(shopware_id)

                if not shopware_data:
                    stats["not_in_shopware"] += 1
                    stats["errors"].append({
                        "item": item_code,
                        "error": "Product not found in Shopware",
                        "shopware_id": shopware_id
                    })
                    continue

                # Compare (skip categories if not comparing them)
                comparison = compare_item_with_shopware(erpnext_item, shopware_data, compare_categories=compare_categories)

                if comparison["needs_sync"]:
                    stats["out_of_sync"] += 1
                    out_of_sync_items.append({
                        "item_code": item_code,
                        "differences": comparison["differences"],
                        "details": comparison["details"]
                    })

                    # Sync if not dry run
                    if not dry_run:
                        try:
                            # Temporarily disable image sync unless explicitly requested
                            original_image_setting = getattr(setting, 'sync_images_to_shopware', True)
                            if not sync_images:
                                setting.sync_images_to_shopware = False

                            upload_erpnext_item_to_shopware(doc=erpnext_item)

                            # Restore setting
                            setting.sync_images_to_shopware = original_image_setting

                            stats["synced"] += 1
                            synced_items.append({
                                "item_code": item_code,
                                "differences": comparison["differences"]
                            })
                        except Exception as sync_error:
                            stats["sync_failed"] += 1
                            stats["errors"].append({
                                "item": item_code,
                                "error": str(sync_error)[:200]
                            })
                else:
                    stats["in_sync"] += 1

            except Exception as e:
                stats["errors"].append({
                    "item": item_code,
                    "error": str(e)[:200]
                })

        # Commit periodically to avoid long transactions
        if stats["total_checked"] % 20 == 0:
            frappe.db.commit()

    # Optionally sync unlinked items
    if include_unlinked and not dry_run:
        synced_item_codes = [e.erpnext_item_code for e in ecom_items]

        # Find unlinked, non-disabled items
        unlinked_items = frappe.get_all(
            "Item",
            filters={
                "disabled": 0,
                "has_variants": 0,
                "variant_of": ["is", "not set"],
                "name": ["not in", synced_item_codes] if synced_item_codes else ["is", "set"]
            },
            pluck="name",
            limit=int(limit) - stats["total_checked"]
        )

        for item_code in unlinked_items:
            try:
                item = frappe.get_doc("Item", item_code)
                upload_erpnext_item_to_shopware(doc=item)
                stats["newly_synced"] += 1
            except Exception as e:
                stats["errors"].append({
                    "item": item_code,
                    "error": f"New sync failed: {str(e)[:150]}"
                })

    frappe.db.commit()

    # Build result
    result = {
        "success": True,
        "dry_run": dry_run,
        "statistics": stats,
        "message": (
            f"Checked {stats['total_checked']} items: "
            f"{stats['in_sync']} in sync, "
            f"{stats['out_of_sync']} out of sync"
        )
    }

    if dry_run:
        result["out_of_sync_items"] = out_of_sync_items[:50]  # Limit for response size
        result["message"] += " (DRY RUN - no changes made)"
    else:
        result["synced_items"] = synced_items[:50]
        result["message"] += f", {stats['synced']} synced"

    if stats["errors"]:
        result["errors"] = stats["errors"][:20]  # Limit errors in response

    create_shopware_log(
        status="Success",
        message=result["message"],
        request_data={"limit": limit, "dry_run": dry_run, "include_unlinked": include_unlinked},
        method="reconcile_erpnext_with_shopware"
    )

    return result


@frappe.whitelist()
def reconcile_all_to_shopware(
    limit: int = 100,
    dry_run: bool = False,
    sync_images: bool = False,
    compare_categories: bool = True
) -> Dict[str, Any]:
    """
    Convenience wrapper for reconcile_erpnext_with_shopware.

    This is a simpler interface that can be called from the frontend
    without needing to pass the client parameter.

    Args:
        limit: Maximum number of items to process
        dry_run: If True, only report differences without syncing
        sync_images: If True, also sync images for changed items
        compare_categories: If True, also compare and sync categories (default True)

    Returns:
        dict: Reconciliation results
    """
    # Convert string parameters from frontend
    if isinstance(dry_run, str):
        dry_run = dry_run.lower() in ("true", "1", "yes")
    if isinstance(sync_images, str):
        sync_images = sync_images.lower() in ("true", "1", "yes")
    if isinstance(compare_categories, str):
        compare_categories = compare_categories.lower() in ("true", "1", "yes")

    return reconcile_erpnext_with_shopware(
        limit=int(limit),
        dry_run=dry_run,
        sync_images=sync_images,
        include_unlinked=False,
        compare_categories=compare_categories
    )


@frappe.whitelist()
def enqueue_full_reconciliation(
    limit: int = 500,
    dry_run: bool = False,
    sync_images: bool = False,
    include_unlinked: bool = False
) -> Dict[str, Any]:
    """
    Enqueue a full reconciliation job to run in the background.

    Use this for large reconciliations that would timeout in a web request.

    Args:
        limit: Maximum number of items to process
        dry_run: If True, only report differences without syncing
        sync_images: If True, also sync images for changed items
        include_unlinked: If True, also sync items not yet in Shopware

    Returns:
        dict: Job enqueue status
    """
    # Convert string parameters
    if isinstance(dry_run, str):
        dry_run = dry_run.lower() in ("true", "1", "yes")
    if isinstance(sync_images, str):
        sync_images = sync_images.lower() in ("true", "1", "yes")
    if isinstance(include_unlinked, str):
        include_unlinked = include_unlinked.lower() in ("true", "1", "yes")

    frappe.enqueue(
        "ecommerce_integrations.shopware6.product_export.reconcile_erpnext_with_shopware",
        queue="long",
        timeout=3600,  # 1 hour
        job_name=f"shopware6_reconciliation_{frappe.utils.now()}",
        limit=int(limit),
        dry_run=dry_run,
        sync_images=sync_images,
        include_unlinked=include_unlinked
    )

    return {
        "success": True,
        "message": f"Reconciliation job enqueued. Processing up to {limit} items.",
        "job_queue": "long",
        "dry_run": dry_run
    }
