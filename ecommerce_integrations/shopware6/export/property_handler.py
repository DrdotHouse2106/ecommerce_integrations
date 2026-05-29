"""
Shopware 6 Property Handler

Manages property groups, property options, and custom fields for products.
Handles both product properties (filterable attributes) and variant options.
"""

from typing import Any

import frappe
from frappe.utils import cstr
from lib_shopware6_api_base import HEADER_index_asynchronously

from ecommerce_integrations.ecommerce_integrations.ecommerce_custom_fields import PROP_IS_SURCHARGE
from ecommerce_integrations.property_utils import (
    coerce_custom_field_value,
    get_ecommerce_properties,
    shopware_custom_field_name,
)
from ecommerce_integrations.shopware6.base.cache_manager import get_cache
from ecommerce_integrations.shopware6.constants import (
    PRODUCT_CUSTOM_FIELDS_MAP,
    SHOPWARE_CUSTOM_FIELD_SET_NAME,
    WEIGHT_TO_ERPNEXT_UOM_MAP,
)
from ecommerce_integrations.shopware6.export.utils import (
    generate_uuid,
    get_component_for_field_type,
    get_field_mappings_cached,
)


def build_custom_fields_from_mappings() -> list[dict[str, Any]]:
    """
    Build Shopware custom fields array from configured mappings.

    Returns:
        List of custom field definitions for the custom field set
    """
    mappings = get_field_mappings_cached()
    custom_fields = []
    position = 1

    for _erpnext_field, config in mappings.items():
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


def ensure_shopware_custom_field_set(client) -> str | None:
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

        # Build custom fields from configured field mappings (Shopware Field Mapping DocType)
        custom_fields = build_custom_fields_from_mappings()

        if not custom_fields:
            logger = get_logger("property_handler")
            logger.warning("No custom field mappings configured. Set up Shopware Field Mapping to define custom fields.")

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

    except Exception:
        get_logger().error("Failed to create/get Shopware custom field set", persist=False)
        return None


def get_item_custom_fields(erpnext_item) -> dict[str, Any]:
    """
    Get custom field values from ERPNext Item for Shopware sync.

    Merges custom fields from all sources:
    1. PRODUCT_CUSTOM_FIELDS_MAP (hardcoded mappings including AI fields)
    2. Configurable field mappings from Shopware Setting
    3. ecommerce_properties table (universal key-value table, filtered by sync_to_shopware)

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

    for row in get_ecommerce_properties(erpnext_item):
        if row.property_type == 'Custom Field' and row.property_value:
            field_name = shopware_custom_field_name(row.property_name)
            custom_fields[field_name] = coerce_custom_field_value(cstr(row.property_value).strip())

    return custom_fields


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
        get_logger().error(f"Failed to get/create variant option {group_name}: {e}", persist=False)
        return None


def get_template_item_attributes(template_item) -> list[dict[str, Any]]:
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

            try:
                attr_doc = frappe.get_doc("Item Attribute", attr_name)
            except frappe.DoesNotExistError:
                # Auto-create missing Item Attribute
                frappe.logger("shopware6").warning(
                    f"Item Attribute '{attr_name}' not found, creating it automatically"
                )
                attr_doc = frappe.get_doc({
                    "doctype": "Item Attribute",
                    "attribute_name": attr_name,
                    "numeric_values": 0
                })
                attr_doc.insert(ignore_permissions=True)
                frappe.db.commit()
                frappe.logger("shopware6").info(
                    f"Created Item Attribute '{attr_name}' for template {template_item.name}"
                )

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
            except frappe.DoesNotExistError:
                # Auto-create missing Item Attribute
                frappe.logger("shopware6").warning(
                    f"Variant Item Attribute '{attr_name}' not found, creating it automatically"
                )
                attr_doc = frappe.get_doc({
                    "doctype": "Item Attribute",
                    "attribute_name": attr_name,
                    "numeric_values": 0
                })
                attr_doc.insert(ignore_permissions=True)
                frappe.db.commit()
                frappe.logger("shopware6").info(
                    f"Created Item Attribute '{attr_name}' for template {template_item.name}"
                )

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

    return attributes


def get_variant_attribute_values(variant_item) -> dict[str, str]:
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

    # Sort by idx to maintain attribute order from ERPNext
    for attr_row in sorted(variant_item.attributes, key=lambda x: x.idx):
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


def get_item_properties(erpnext_item) -> list[dict[str, str]]:
    """
    Get property values for Shopware from ERPNext Item.

    Priority:
    1. Read from ecommerce_properties table (universal, filtered by sync_to_shopware)
    2. Fallback to old configurable mappings (for backwards compatibility)

    Args:
        erpnext_item: ERPNext Item document

    Returns:
        List of {group_name, option_value, filterable} dicts
    """
    properties = []

    for row in sorted(get_ecommerce_properties(erpnext_item), key=lambda x: getattr(x, 'idx', 0)):
        if row.property_type in ('Property', 'Text') and row.property_value:
            properties.append({
                "group_name": row.property_name,
                "option_value": cstr(row.property_value).strip(),
                "filterable": bool(getattr(row, 'filterable', 0))
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
                except BaseException as e:
                    # Catch ALL exceptions including ShopwareAPIError
                    # Don't let property deletion errors break the sync
                    get_logger().warning(
                        f"Failed to delete property {prop_id} from product {product_id}: {str(e)[:100]}",
                        persist=False
                    )
                    continue  # Continue with next property

        return True

    except BaseException as e:
        get_logger().warning(
            f"Failed to clear properties for product {product_id}: {str(e)[:100]}",
            persist=False
        )
        return False


def clear_product_options(client, product_id: str) -> bool:
    """
    Clear all variant option assignments from a product in Shopware.

    This ensures that when a variant is updated, old/orphaned option
    assignments are removed before new ones are set. This prevents
    duplicate options from different property groups (e.g., a generic "Farbe" and a manufacturer-prefixed "<Hersteller> Farbe").

    Args:
        client: Shopware API client
        product_id: Shopware product UUID

    Returns:
        True if successful
    """
    try:
        response = client.request_get(f"product/{product_id}?associations[options][]")
        product_data = response.get("data", {})
        options = product_data.get("options", [])

        if not options:
            return True

        # Delete each option assignment
        for opt in options:
            opt_id = opt.get("id")
            if opt_id:
                try:
                    client.request_delete(f"product/{product_id}/options/{opt_id}")
                except BaseException as e:
                    # Catch ALL exceptions including ShopwareAPIError
                    # Don't let option deletion errors break the sync
                    get_logger().warning(
                        f"Failed to delete option {opt_id} from product {product_id}: {str(e)[:100]}",
                        persist=False
                    )
                    continue  # Continue with next option

        return True

    except BaseException as e:
        get_logger().warning(
            f"Failed to clear options for product {product_id}: {str(e)[:100]}",
            persist=False
        )
        return False


def ensure_surcharge_property(item_code: str) -> bool:
    """
    Ensure an item has the is_surcharge property set if it's a Surcharge item.

    Surcharge items are detected by:
    - is_sales_item = 0 (cannot be sold individually)

    This function checks if the property already exists and adds it if missing.

    Args:
        item_code: ERPNext Item code

    Returns:
        True if property exists or was added, False on error
    """
    try:
        item = frappe.get_doc("Item", item_code)

        # Check if item is a Surcharge item (is_sales_item = 0)
        if item.is_sales_item:
            return True  # Not a Surcharge item, nothing to do

        # Check if is_surcharge property already exists
        existing_props = getattr(item, 'ecommerce_properties', []) or []
        for prop in existing_props:
            if prop.property_name == PROP_IS_SURCHARGE:
                return True  # Already has the property

        # Add the is_surcharge property (synced to all ecommerce backends)
        item.append('ecommerce_properties', {
            'property_name': PROP_IS_SURCHARGE,
            'property_type': 'Custom Field',
            'property_value': 'true',
            'sync_to_shopware': 1,
            'sync_to_medusa': 1,
        })
        item.save(ignore_permissions=True)

        frappe.logger("shopware6").info(
            f"Added is_surcharge property to item {item_code}"
        )
        return True

    except Exception:
        get_logger().error("Error occurred", persist=False)
        return False


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


def cleanup_orphaned_shopware_properties(client, dry_run: bool = True) -> dict[str, Any]:
    """
    Remove property groups and options from Shopware that are not used by any ERPNext item.

    This cleans up orphaned properties that were created but are no longer referenced.
    Only removes properties that:
    1. Are not assigned to any product in Shopware
    2. Are not used by any Item Attribute in ERPNext

    Args:
        client: Shopware API client
        dry_run: If True, only report what would be deleted without actually deleting

    Returns:
        Dict with cleanup statistics
    """
    stats = {
        "groups_checked": 0,
        "groups_deleted": 0,
        "options_checked": 0,
        "options_deleted": 0,
        "errors": [],
        "dry_run": dry_run
    }

    try:
        # Get all property groups from Shopware
        response = client.request_post(
            "search/property-group",
            {
                "limit": 500,
                "associations": {
                    "options": {}
                }
            }
        )
        groups = response.get("data", [])
        stats["groups_checked"] = len(groups)

        # Get all Item Attributes from ERPNext (these should exist as property groups)
        erpnext_attributes = set()
        attrs = frappe.get_all("Item Attribute", fields=["attribute_name"])
        for attr in attrs:
            erpnext_attributes.add(attr.attribute_name)

        # Also get attributes from ecommerce_properties tables
        prop_names = frappe.db.sql("""
            SELECT DISTINCT property_name
            FROM `tabItem Ecommerce Property`
            WHERE property_type = 'Property' AND sync_to_shopware = 1
        """, as_list=True)
        for row in prop_names:
            if row and row[0]:
                erpnext_attributes.add(row[0])

        frappe.logger("shopware6").info(
            f"Checking {len(groups)} Shopware property groups against {len(erpnext_attributes)} ERPNext attributes"
        )

        for group in groups:
            group_name = group.get("name", "")
            group_id = group.get("id")
            options = group.get("options", [])

            # Skip system property groups (Shopware internal)
            if group_name.startswith("_") or not group_name:
                continue

            # Check if this group is used by any ERPNext item
            if group_name not in erpnext_attributes:
                # Check if it has any products assigned in Shopware
                try:
                    prod_response = client.request_post(
                        "search/product",
                        {
                            "limit": 1,
                            "filter": [
                                {
                                    "type": "equals",
                                    "field": "properties.groupId",
                                    "value": group_id
                                }
                            ]
                        }
                    )
                    product_count = prod_response.get("total", 0)

                    if product_count == 0:
                        # No products use this property group - safe to delete
                        if not dry_run:
                            try:
                                client.request_delete(f"property-group/{group_id}")
                                stats["groups_deleted"] += 1
                                frappe.logger("shopware6").info(
                                    f"Deleted orphaned property group: {group_name}"
                                )
                            except Exception as e:
                                stats["errors"].append({
                                    "type": "group_delete",
                                    "name": group_name,
                                    "error": str(e)
                                })
                        else:
                            stats["groups_deleted"] += 1
                            frappe.logger("shopware6").info(
                                f"Would delete orphaned property group: {group_name}"
                            )
                except Exception as e:
                    stats["errors"].append({
                        "type": "group_check",
                        "name": group_name,
                        "error": str(e)
                    })

            # Check individual options within used groups
            stats["options_checked"] += len(options)

        frappe.logger("shopware6").info(
            f"Property cleanup {'(dry run)' if dry_run else ''}: "
            f"{stats['groups_deleted']} groups, {stats['options_deleted']} options"
        )

    except Exception as e:
        stats["errors"].append({
            "type": "general",
            "error": str(e)
        })
        frappe.logger("shopware6").error(f"Property cleanup failed: {e!s}")

    return stats


def get_logger():
    """Get the Shopware logger."""
    from ecommerce_integrations.shopware6.utils import get_logger as _get_logger
    return _get_logger("PropertyHandler")


# =============================================================================
# BATCH ORPHAN PROPERTY CLEANUP (using Shopware Sync API for faster deletion)
# =============================================================================

# Rate limiting to avoid overwhelming Shopware API
PROPERTY_BATCH_SIZE = 100  # Options per sync request
PROPERTY_GROUP_BATCH_SIZE = 50  # Groups per sync request
PROPERTY_BATCH_DELAY = 0.05  # 50ms delay between batches


def _log_failed_property_items(failed_ids: list[str], operation: str, error_msg: str) -> None:
    """
    Persist failed property sync items to database for later retry.

    Args:
        failed_ids: List of Shopware property IDs that failed
        operation: Type of operation (option_delete, group_delete)
        error_msg: Error message describing the failure
    """
    if not failed_ids:
        return

    try:
        from ecommerce_integrations.shopware6.utils import create_shopware_log
        create_shopware_log(
            status="Error",
            method=f"property_batch.{operation}",
            message=f"Failed to delete {len(failed_ids)} properties: {error_msg}",
            request_data={
                "operation": operation,
                "failed_count": len(failed_ids),
                "failed_ids": failed_ids[:100],
                "retry_available": True
            }
        )
    except Exception:
        pass


def cleanup_orphaned_properties_batch(client) -> dict[str, Any]:
    """
    Batch-delete orphaned property groups using Shopware Sync API.

    Collects all orphaned property groups first, then deletes them in batches.

    Args:
        client: Shopware API client

    Returns:
        Dict with {"success": bool, "statistics": dict}
    """
    import time

    from ecommerce_integrations.shopware6.utils import get_logger as _get_logger
    logger = _get_logger("cleanup_orphaned_properties_batch")

    stats = {
        "groups_checked": 0,
        "groups_deleted": 0,
        "options_deleted": 0,
        "errors": [],
        "failed_items": []  # Track failed deletions for potential retry
    }

    try:
        # Get all property groups from Shopware
        response = client.request_post(
            "search/property-group",
            {
                "limit": 500,
                "associations": {"options": {}}
            }
        )
        groups = response.get("data", [])
        stats["groups_checked"] = len(groups)

        # Get all Item Attributes from ERPNext
        erpnext_attributes = set()
        attrs = frappe.get_all("Item Attribute", fields=["attribute_name"])
        for attr in attrs:
            erpnext_attributes.add(attr.attribute_name)

        # Also get attributes from ecommerce_properties tables
        prop_names = frappe.db.sql("""
            SELECT DISTINCT property_name
            FROM `tabItem Ecommerce Property`
            WHERE property_type = 'Property' AND sync_to_shopware = 1
        """, as_list=True)
        for row in prop_names:
            if row and row[0]:
                erpnext_attributes.add(row[0])

        logger.info(f"Checking {len(groups)} property groups against {len(erpnext_attributes)} ERPNext attributes")

        # Collect groups to delete
        groups_to_delete = []
        options_to_delete = []

        for group in groups:
            group_name = group.get("name", "")
            group_id = group.get("id")
            options = group.get("options", [])

            # Skip system property groups
            if group_name.startswith("_") or not group_name:
                continue

            # Check if used in ERPNext
            if group_name not in erpnext_attributes:
                # Check if products use this property
                try:
                    prod_response = client.request_post(
                        "search/product",
                        {
                            "limit": 1,
                            "filter": [
                                {"type": "equals", "field": "properties.groupId", "value": group_id}
                            ]
                        }
                    )
                    product_count = prod_response.get("total", 0)

                    if product_count == 0:
                        # Delete all options first (required before deleting group)
                        for opt in options:
                            options_to_delete.append({"id": opt.get("id")})
                        groups_to_delete.append({"id": group_id})

                except Exception as e:
                    stats["errors"].append({
                        "type": "check",
                        "group": group_name,
                        "error": str(e)[:100]
                    })

        if not groups_to_delete:
            logger.info("No orphaned property groups found")
            return {"success": True, "statistics": stats}

        logger.info(f"Found {len(groups_to_delete)} orphaned property groups with {len(options_to_delete)} options to delete")

        # Batch delete options first
        if options_to_delete:
            total_option_batches = (len(options_to_delete) + PROPERTY_BATCH_SIZE - 1) // PROPERTY_BATCH_SIZE
            for i in range(0, len(options_to_delete), PROPERTY_BATCH_SIZE):
                batch = options_to_delete[i:i + PROPERTY_BATCH_SIZE]
                batch_num = i // PROPERTY_BATCH_SIZE + 1
                try:
                    sync_payload = {
                        "delete-orphan-options": {
                            "entity": "property_group_option",
                            "action": "delete",
                            "payload": batch
                        }
                    }
                    response = client.request_post(
                        "_action/sync",
                        sync_payload,
                        update_header_fields=HEADER_index_asynchronously
                    )

                    # Validate Sync API response
                    not_found = response.get("notFound", []) if response else []
                    successful = len(batch) - len(not_found)
                    stats["options_deleted"] += successful

                    if not_found:
                        logger.warning(f"Sync API: {len(not_found)} options not found")
                        for nf in not_found:
                            stats["failed_items"].append({"type": "option", "id": nf})

                except Exception as e:
                    stats["errors"].append({
                        "type": "options_delete",
                        "batch": batch_num,
                        "error": str(e)[:150]
                    })
                    # Track failed items for potential retry
                    failed_ids = [opt.get("id") for opt in batch]
                    for opt in batch:
                        stats["failed_items"].append({"type": "option", "id": opt.get("id")})
                    # Persist to database for later retry
                    _log_failed_property_items(failed_ids, "option_delete", str(e)[:150])

                # Rate limiting between batches
                if batch_num < total_option_batches:
                    time.sleep(PROPERTY_BATCH_DELAY)

        # Batch delete groups
        if groups_to_delete:
            total_group_batches = (len(groups_to_delete) + PROPERTY_GROUP_BATCH_SIZE - 1) // PROPERTY_GROUP_BATCH_SIZE
            for i in range(0, len(groups_to_delete), PROPERTY_GROUP_BATCH_SIZE):
                batch = groups_to_delete[i:i + PROPERTY_GROUP_BATCH_SIZE]
                batch_num = i // PROPERTY_GROUP_BATCH_SIZE + 1
                try:
                    sync_payload = {
                        "delete-orphan-groups": {
                            "entity": "property_group",
                            "action": "delete",
                            "payload": batch
                        }
                    }
                    response = client.request_post(
                        "_action/sync",
                        sync_payload,
                        update_header_fields=HEADER_index_asynchronously
                    )

                    # Validate Sync API response
                    not_found = response.get("notFound", []) if response else []
                    successful = len(batch) - len(not_found)
                    stats["groups_deleted"] += successful

                    if not_found:
                        logger.warning(f"Sync API: {len(not_found)} groups not found")
                        for nf in not_found:
                            stats["failed_items"].append({"type": "group", "id": nf})

                except Exception as e:
                    stats["errors"].append({
                        "type": "groups_delete",
                        "batch": batch_num,
                        "error": str(e)[:150]
                    })
                    # Track failed items for potential retry
                    failed_ids = [grp.get("id") for grp in batch]
                    for grp in batch:
                        stats["failed_items"].append({"type": "group", "id": grp.get("id")})
                    # Persist to database for later retry
                    _log_failed_property_items(failed_ids, "group_delete", str(e)[:150])

                # Rate limiting between batches
                if batch_num < total_group_batches:
                    time.sleep(PROPERTY_BATCH_DELAY)

        logger.success(
            f"Property cleanup complete: {stats['groups_deleted']} groups, "
            f"{stats['options_deleted']} options deleted"
        )

    except Exception as e:
        stats["errors"].append({"type": "general", "error": str(e)[:200]})
        logger.error(f"Property cleanup failed: {e}")

    # Standardized return: success=False if there were errors
    return {"success": len(stats["errors"]) == 0, "statistics": stats}
