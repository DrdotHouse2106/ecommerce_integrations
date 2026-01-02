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
    SHOPWARE_CUSTOM_FIELD_IS_MEHRPREIS,
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
                },
                {
                    "id": generate_uuid(f"custom_field_{SHOPWARE_CUSTOM_FIELD_IS_MEHRPREIS}"),
                    "name": SHOPWARE_CUSTOM_FIELD_IS_MEHRPREIS,
                    "type": "bool",
                    "config": {
                        "label": {"en-GB": "Is Surcharge Product", "de-DE": "Ist Mehrpreis"},
                        "customFieldPosition": 4,
                        "componentName": "sw-field",
                        "helpText": {"en-GB": "Product cannot be sold individually, only as accessory", "de-DE": "Produkt kann nicht einzeln gekauft werden, nur als Zubehör"}
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
        get_logger().error("Failed to create/get Shopware custom field set", persist=False)
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
            value = cstr(row.property_value).strip()

            # Convert boolean strings to actual booleans for Shopware bool fields
            if value.lower() in ('true', '1', 'yes'):
                custom_fields[shopware_field_name] = True
            elif value.lower() in ('false', '0', 'no'):
                custom_fields[shopware_field_name] = False
            else:
                custom_fields[shopware_field_name] = value

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
        get_logger().error("Failed to get/create PropertyGroup {group_name}", persist=False)
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
        get_logger().error("Failed to get/create PropertyOption {group_name}", persist=False)
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
        get_logger().error("Failed to get/create variant option {group_name}", persist=False)
        return None


def get_template_item_attributes(template_item) -> List[Dict[str, Any]]:
    """
    Get all Item Attributes for a template item, including attributes from variants.

    This function scans both the template's attribute definitions AND all variant
    items to find any additional attributes that may be defined on variants but
    not on the template itself.

    Args:
        template_item: ERPNext Item document with has_variants=1

    Returns:
        List of attribute info dicts with name and possible values
    """
    attributes = []
    seen_attributes = set()

    # First, get attributes defined on template
    if template_item.attributes:
        for attr_row in template_item.attributes:
            attr_name = attr_row.attribute
            seen_attributes.add(attr_name)

            attr_doc = frappe.get_doc("Item Attribute", attr_name)
            values = []
            if not attr_doc.numeric_values:
                values = [v.attribute_value for v in attr_doc.item_attribute_values]

            attributes.append({
                "name": attr_name,
                "values": values,
            })

    # Second, scan variants for additional attributes not on template
    # This handles cases where variants have more attributes than the template defines
    variant_attrs = frappe.db.sql("""
        SELECT DISTINCT iva.attribute
        FROM `tabItem Variant Attribute` iva
        INNER JOIN `tabItem` i ON i.name = iva.parent
        WHERE i.variant_of = %s AND i.disabled = 0
    """, (template_item.name,), as_dict=True)

    for row in variant_attrs:
        attr_name = row.attribute
        if attr_name not in seen_attributes:
            seen_attributes.add(attr_name)

            try:
                attr_doc = frappe.get_doc("Item Attribute", attr_name)
                values = []
                if not attr_doc.numeric_values:
                    values = [v.attribute_value for v in attr_doc.item_attribute_values]

                attributes.append({
                    "name": attr_name,
                    "values": values,
                })
                frappe.logger("shopware6").info(
                    f"Added variant attribute '{attr_name}' for template {template_item.name}"
                )
            except Exception as e:
                get_logger().error("Error occurred", persist=False)

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


def clear_product_properties(client, product_id: str) -> bool:
    """
    Clear all property assignments from a product in Shopware.

    This ensures that when properties are updated, old/stale property
    assignments are removed before new ones are set.

    Args:
        client: Shopware API client
        product_id: Shopware product UUID

    Returns:
        True if successful
    """
    try:
        response = client.request_get(f"product/{product_id}?associations[properties][]")
        product_data = response.get("data", {})
        properties = product_data.get("properties", [])

        if not properties:
            return True

        # Delete each property assignment
        for prop in properties:
            prop_id = prop.get("id")
            if prop_id:
                try:
                    client.request_delete(f"product/{product_id}/properties/{prop_id}")
                except Exception as e:
                    get_logger().error("Error occurred", persist=False)

        return True

    except Exception as e:
        get_logger().error("Error occurred", persist=False)
        return False


def ensure_mehrpreis_property(item_code: str) -> bool:
    """
    Ensure an item has the is_mehrpreis property set if it's a Mehrpreis item.

    Mehrpreis items are detected by:
    - is_sales_item = 0 (cannot be sold individually)
    - OR brand = 'vendor' and specific product codes (legacy detection)

    This function checks if the property already exists and adds it if missing.

    Args:
        item_code: ERPNext Item code

    Returns:
        True if property exists or was added, False on error
    """
    try:
        item = frappe.get_doc("Item", item_code)

        # Check if item is a Mehrpreis item (is_sales_item = 0)
        if item.is_sales_item:
            return True  # Not a Mehrpreis item, nothing to do

        # Check if is_mehrpreis property already exists
        existing_props = getattr(item, 'shopware_properties', []) or []
        for prop in existing_props:
            if prop.property_name == 'is_mehrpreis':
                return True  # Already has the property

        # Add the is_mehrpreis property
        item.append('shopware_properties', {
            'property_name': 'is_mehrpreis',
            'property_type': 'Custom Field',
            'property_value': 'true'
        })
        item.save(ignore_permissions=True)

        frappe.logger("shopware6").info(
            f"Added is_mehrpreis property to item {item_code}"
        )
        return True

    except Exception as e:
        get_logger().error("Error occurred", persist=False)
        return False


def sync_mehrpreis_properties_batch(limit: int = 500) -> Dict[str, Any]:
    """
    Batch sync Mehrpreis properties for all items where is_sales_item = 0.

    This can be run during reconciliation to ensure all Mehrpreis items
    have the is_mehrpreis custom field property set.

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

    # Find all items where is_sales_item = 0 and not disabled
    items = frappe.get_all(
        "Item",
        filters={
            "is_sales_item": 0,
            "disabled": 0,
            "has_variants": 0  # Skip template items
        },
        fields=["name"],
        limit=limit
    )

    for item in items:
        stats["checked"] += 1

        try:
            item_doc = frappe.get_doc("Item", item.name)

            # Check if property already exists
            existing_props = getattr(item_doc, 'shopware_properties', []) or []
            has_mehrpreis = any(
                prop.property_name == 'is_mehrpreis'
                for prop in existing_props
            )

            if has_mehrpreis:
                stats["already_set"] += 1
                continue

            # Add the property
            item_doc.append('shopware_properties', {
                'property_name': 'is_mehrpreis',
                'property_type': 'Custom Field',
                'property_value': 'true'
            })
            item_doc.save(ignore_permissions=True)
            stats["added"] += 1

        except Exception as e:
            stats["errors"] += 1
            get_logger().error("Error occurred", persist=False)

        # Commit periodically
        if stats["checked"] % 50 == 0:
            frappe.db.commit()

    frappe.db.commit()

    frappe.logger("shopware6").info(
        f"Mehrpreis batch sync: {stats['checked']} checked, "
        f"{stats['added']} added, {stats['already_set']} already set, "
        f"{stats['errors']} errors"
    )

    return stats
