"""
Shopware 6 Property Handler

Manages property groups, property options, and custom fields for products.
Handles both product properties (filterable attributes) and variant options.
"""

from typing import Any, Dict, List, Optional

import frappe
from frappe.utils import cstr

from ecommerce_integrations.shopware6.connection import temp_shopware_session
from ecommerce_integrations.shopware6.constants import (
    SETTING_DOCTYPE,
    SHOPWARE_CUSTOM_FIELD_SET_NAME,
    SHOPWARE_CUSTOM_FIELD_ZUBEHOER,
    SHOPWARE_CUSTOM_FIELD_SICHERHEITSBLAETTER,
    SHOPWARE_CUSTOM_FIELD_PRODUKTBLAETTER,
    PRODUCT_CUSTOM_FIELDS_MAP,
    WEIGHT_TO_ERPNEXT_UOM_MAP,
)
from ecommerce_integrations.shopware6.base.cache_manager import get_cache
from ecommerce_integrations.shopware6.export.utils import (
    generate_uuid,
    get_field_mappings_cached,
    get_component_for_field_type,
)


def build_custom_fields_from_mappings() -> List[Dict[str, Any]]:
    """
    Build Shopware custom fields array from configured mappings.

    Returns:
        List of custom field definitions for the custom field set
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
                    "componentName": get_component_for_field_type(config['shopware_field_type'])
                }
            })
            position += 1

    return custom_fields


def ensure_shopware_custom_field_set(client) -> Optional[str]:
    """
    Ensure the ERPNext custom field set exists in Shopware.

    Dynamically creates custom fields based on configured field mappings.
    Falls back to legacy hardcoded fields if no mappings are configured.

    Args:
        client: Shopware API client

    Returns:
        Custom field set ID if successful, None otherwise
    """
    cache = get_cache()
    cached_id = cache.get("custom_field_set", SHOPWARE_CUSTOM_FIELD_SET_NAME)
    if cached_id:
        return cached_id

    try:
        # Search for existing custom field set
        response = client.request_post(
            "search/custom-field-set",
            {"filter": [{"type": "equals", "field": "name", "value": SHOPWARE_CUSTOM_FIELD_SET_NAME}]}
        )
        sets = response.get("data", [])

        if sets:
            set_id = sets[0]["id"]
            cache.set("custom_field_set", SHOPWARE_CUSTOM_FIELD_SET_NAME, set_id)
            return set_id

        # Build custom fields from mappings
        custom_fields = build_custom_fields_from_mappings()

        # Fallback to legacy hardcoded fields if no mappings configured
        if not custom_fields:
            custom_fields = [
                {
                    "id": generate_uuid(f"custom_field_{SHOPWARE_CUSTOM_FIELD_ZUBEHOER}"),
                    "name": SHOPWARE_CUSTOM_FIELD_ZUBEHOER,
                    "type": "html",
                    "config": {
                        "label": {"en-GB": "Accessories", "de-DE": "Zubehör"},
                        "customFieldPosition": 1,
                        "componentName": "sw-text-editor"
                    }
                },
                {
                    "id": generate_uuid(f"custom_field_{SHOPWARE_CUSTOM_FIELD_SICHERHEITSBLAETTER}"),
                    "name": SHOPWARE_CUSTOM_FIELD_SICHERHEITSBLAETTER,
                    "type": "html",
                    "config": {
                        "label": {"en-GB": "Safety Data Sheets", "de-DE": "Sicherheitsblätter"},
                        "customFieldPosition": 2,
                        "componentName": "sw-text-editor"
                    }
                },
                {
                    "id": generate_uuid(f"custom_field_{SHOPWARE_CUSTOM_FIELD_PRODUKTBLAETTER}"),
                    "name": SHOPWARE_CUSTOM_FIELD_PRODUKTBLAETTER,
                    "type": "html",
                    "config": {
                        "label": {"en-GB": "Product Data Sheets", "de-DE": "Produktblätter"},
                        "customFieldPosition": 3,
                        "componentName": "sw-text-editor"
                    }
                }
            ]

        # Create new custom field set
        set_id = generate_uuid(f"custom_field_set_{SHOPWARE_CUSTOM_FIELD_SET_NAME}")
        payload = {
            "id": set_id,
            "name": SHOPWARE_CUSTOM_FIELD_SET_NAME,
            "config": {
                "label": {"en-GB": "ERPNext Product Fields", "de-DE": "ERPNext Produktfelder"}
            },
            "customFields": custom_fields,
            "relations": [{
                "id": generate_uuid(f"custom_field_set_relation_{SHOPWARE_CUSTOM_FIELD_SET_NAME}_product"),
                "entityName": "product"
            }]
        }

        client.request_post("custom-field-set", payload)
        cache.set("custom_field_set", SHOPWARE_CUSTOM_FIELD_SET_NAME, set_id)

        frappe.logger().info(f"Created Shopware custom field set: {SHOPWARE_CUSTOM_FIELD_SET_NAME}")
        return set_id

    except Exception as e:
        frappe.log_error(f"Failed to create/get Shopware custom field set: {e}")
        return None


def get_item_custom_fields(erpnext_item) -> Dict[str, Any]:
    """
    Get custom field values from ERPNext Item for Shopware sync.

    Merges custom fields from all sources:
    1. PRODUCT_CUSTOM_FIELDS_MAP (hardcoded mappings including AI fields)
    2. Configurable field mappings from Shopware Setting
    3. shopware_properties table (flexible key-value table)

    Args:
        erpnext_item: ERPNext Item document

    Returns:
        Dict with Shopware custom field names as keys
    """
    custom_fields = {}

    # Source 1: Hardcoded mappings (AI fields, Zubehoer, etc.)
    for erpnext_field, shopware_field in PRODUCT_CUSTOM_FIELDS_MAP.items():
        value = getattr(erpnext_item, erpnext_field, None)
        if value:
            custom_fields[shopware_field] = cstr(value).strip()

    # Source 2: Configurable field mappings
    mappings = get_field_mappings_cached()
    for erpnext_field, config in mappings.items():
        if config['mapping_type'] == 'Custom Field':
            value = getattr(erpnext_item, erpnext_field, None)
            if value:
                custom_fields[config['shopware_field']] = cstr(value).strip()

    # Source 3: shopware_properties table
    properties_table = getattr(erpnext_item, 'shopware_properties', None) or []
    for row in properties_table:
        if row.property_type == 'Custom Field' and row.property_value:
            shopware_field_name = f"erpnext_{row.property_name.lower().replace(' ', '_')}"
            custom_fields[shopware_field_name] = cstr(row.property_value).strip()

    return custom_fields


def get_or_create_property_group(client, group_name: str) -> Optional[str]:
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
        groups = response.get("data", [])

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
        frappe.log_error(f"Failed to get/create PropertyGroup {group_name}: {e}")
        return None


def get_or_create_property_option(client, group_id: str, group_name: str, option_value: str) -> Optional[str]:
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
        options = response.get("data", [])

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
        frappe.log_error(f"Failed to get/create PropertyOption {group_name}:{option_value}: {e}")
        return None


def get_or_create_variant_option(client, group_id: str, group_name: str, option_value: str) -> Optional[str]:
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
        options = response.get("data", [])

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
        frappe.log_error(f"Failed to get/create variant option {group_name}:{option_value}: {e}")
        return None


def get_template_item_attributes(template_item) -> List[Dict[str, Any]]:
    """
    Get all Item Attributes defined for a template item.

    Args:
        template_item: ERPNext Item document with has_variants=1

    Returns:
        List of attribute info dicts with name and possible values
    """
    attributes = []

    if not template_item.attributes:
        return attributes

    for attr_row in template_item.attributes:
        attr_name = attr_row.attribute
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

    Args:
        variant_item: ERPNext Item document (variant)

    Returns:
        Dict of {attribute_name: attribute_value}
    """
    attr_values = {}

    if not variant_item.attributes:
        return attr_values

    for attr_row in variant_item.attributes:
        attr_values[attr_row.attribute] = attr_row.attribute_value

    return attr_values


def get_shopware_weight_unit(erpnext_uom: str) -> str:
    """
    Convert ERPNext weight UOM to Shopware weight unit.

    Args:
        erpnext_uom: ERPNext UOM name

    Returns:
        Shopware weight unit (kg, g, lb, oz)
    """
    for sw_unit, erp_unit in WEIGHT_TO_ERPNEXT_UOM_MAP.items():
        if erp_unit == erpnext_uom:
            return sw_unit
    return "kg"


def get_item_properties(erpnext_item) -> List[Dict[str, str]]:
    """
    Get property values for Shopware from ERPNext Item.

    Priority:
    1. Read from shopware_properties table (new flexible key-value table)
    2. Fallback to old configurable mappings (for backwards compatibility)

    Args:
        erpnext_item: ERPNext Item document

    Returns:
        List of {group_name, option_value, filterable} dicts
    """
    properties = []

    # Priority 1: Read from shopware_properties table
    properties_table = getattr(erpnext_item, 'shopware_properties', None) or []
    for row in properties_table:
        if row.property_type == 'Property' and row.property_value:
            properties.append({
                "group_name": row.property_name,
                "option_value": cstr(row.property_value).strip(),
                "filterable": bool(getattr(row, 'filterable', True))
            })

    # Priority 2: Fallback to configurable mappings
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
