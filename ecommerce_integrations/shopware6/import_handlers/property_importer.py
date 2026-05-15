"""
Shopware 6 Property Importer

Imports product properties and attributes from Shopware to ERPNext.
Enables bi-directional property synchronization.

Features:
- Import property groups as ERPNext Item Attributes
- Import property options as Item Attribute Values
- Map custom fields to ERPNext custom fields
- Optional toggle via Setting (default: off)

Usage:
    from ecommerce_integrations.shopware6.import_handlers.property_importer import (
        import_properties_from_shopware,
        sync_property_groups_from_shopware,
    )

    # Import properties for a specific product
    import_properties_from_shopware(item_code="ITEM-001", shopware_id="abc123...")

    # Sync all property groups from Shopware
    sync_property_groups_from_shopware()
"""

from typing import Any

import frappe

from ecommerce_integrations.shopware6.connection import get_shopware_client, temp_shopware_session
from ecommerce_integrations.shopware6.constants import MODULE_NAME, SETTING_DOCTYPE
from ecommerce_integrations.shopware6.utils import get_logger


class PropertyImporter:
    """
    Imports properties from Shopware to ERPNext.

    Shopware property structure:
    - Property Groups (e.g., "Color", "Size")
    - Property Options (e.g., "Red", "Blue" under "Color")

    ERPNext mapping:
    - Property Group -> Item Attribute
    - Property Option -> Item Attribute Value
    """

    def __init__(self):
        self.setting = frappe.get_cached_doc(SETTING_DOCTYPE)
        self.logger = get_logger("PropertyImporter")

        # Check if import is enabled
        self.import_enabled = getattr(self.setting, 'import_properties_from_shopware', False)

    def is_import_enabled(self) -> bool:
        """Check if property import is enabled in settings."""
        return self.import_enabled

    def import_properties_for_product(
        self,
        client,
        item_code: str,
        shopware_id: str
    ) -> dict[str, Any]:
        """
        Import properties from a Shopware product to ERPNext Item.

        Args:
            client: Shopware API client
            item_code: ERPNext Item code
            shopware_id: Shopware product ID

        Returns:
            dict with import statistics
        """
        if not self.is_import_enabled():
            return {"imported": 0, "message": "Property import disabled in settings"}

        stats = {
            "item_code": item_code,
            "property_groups_created": 0,
            "property_values_created": 0,
            "custom_fields_updated": 0,
            "errors": [],
        }

        try:
            # Fetch product with properties and custom fields
            response = client.request_get(
                f"product/{shopware_id}?associations[properties][associations][group]={{}}&associations[customFields]={{}}"
            )
            product_data = response.get("data", {})

            if not product_data:
                stats["errors"].append(f"Product {shopware_id} not found in Shopware")
                return stats

            # Import property group values
            properties = product_data.get("properties", [])
            for prop in properties:
                try:
                    group = prop.get("group", {})
                    if not group:
                        continue

                    group_name = group.get("name") or (group.get("translated") or {}).get("name", "")
                    option_name = prop.get("name") or (prop.get("translated") or {}).get("name", "")

                    if group_name and option_name:
                        self._ensure_item_attribute(group_name, option_name)
                        stats["property_values_created"] += 1

                except Exception as e:
                    stats["errors"].append(f"Property import error: {str(e)[:100]}")

            # Import custom fields
            custom_fields = product_data.get("customFields", {})
            if custom_fields:
                updated = self._update_item_custom_fields(item_code, custom_fields)
                stats["custom_fields_updated"] = updated

        except Exception as e:
            stats["errors"].append(f"Product fetch error: {str(e)[:200]}")
            self.logger.error(f"Property import failed for {item_code}", exception=e)

        return stats

    def analyze_properties_for_product(
        self,
        client,
        item_code: str,
        shopware_id: str
    ) -> dict[str, Any]:
        """
        Analyze properties for a product without importing (dry run).

        Args:
            client: Shopware API client
            item_code: ERPNext Item code
            shopware_id: Shopware product ID

        Returns:
            dict with property analysis
        """
        result = {
            "item_code": item_code,
            "shopware_id": shopware_id,
            "properties": {},
            "custom_fields": {},
            "errors": [],
        }

        try:
            response = client.request_get(
                f"product/{shopware_id}?associations[properties][associations][group]={{}}&associations[customFields]={{}}"
            )
            product_data = response.get("data", {})

            if not product_data:
                result["errors"].append(f"Product {shopware_id} not found")
                return result

            # Collect properties
            for prop in product_data.get("properties", []):
                group = prop.get("group", {})
                group_name = group.get("name") or (group.get("translated") or {}).get("name", "")
                option_name = prop.get("name") or (prop.get("translated") or {}).get("name", "")
                if group_name and option_name:
                    result["properties"][group_name] = option_name

            # Collect custom fields
            result["custom_fields"] = product_data.get("customFields", {}) or {}

        except Exception as e:
            result["errors"].append(str(e)[:200])

        return result

    def _ensure_item_attribute(self, group_name: str, option_value: str) -> tuple[str, str]:
        """
        Ensure Item Attribute and Value exist in ERPNext.

        Args:
            group_name: Property group name (becomes Item Attribute)
            option_value: Property option value (becomes Item Attribute Value)

        Returns:
            Tuple of (attribute_name, value)
        """
        # Sanitize names
        attr_name = group_name.strip()[:140]
        option_value = option_value.strip()[:140]

        # Check if attribute exists
        if not frappe.db.exists("Item Attribute", attr_name):
            # Create new attribute
            attr_doc = frappe.get_doc({
                "doctype": "Item Attribute",
                "attribute_name": attr_name,
                "item_attribute_values": [{
                    "attribute_value": option_value,
                    "abbr": option_value[:10]
                }]
            })
            attr_doc.insert(ignore_permissions=True)
            self.logger.info(f"Created Item Attribute: {attr_name}")
        else:
            # Check if value exists
            attr_doc = frappe.get_doc("Item Attribute", attr_name)

            if not attr_doc.numeric_values:
                existing_values = [
                    d.attribute_value.lower()
                    for d in attr_doc.item_attribute_values
                ]

                if option_value.lower() not in existing_values:
                    attr_doc.append("item_attribute_values", {
                        "attribute_value": option_value,
                        "abbr": option_value[:10]
                    })
                    attr_doc.save(ignore_permissions=True)
                    self.logger.info(f"Added value '{option_value}' to attribute '{attr_name}'")

        return attr_name, option_value

    def _update_item_custom_fields(
        self,
        item_code: str,
        custom_fields: dict[str, Any]
    ) -> int:
        """
        Update ERPNext Item custom fields from Shopware custom fields.

        Args:
            item_code: ERPNext Item code
            custom_fields: Shopware custom fields dict

        Returns:
            Number of fields updated
        """
        if not custom_fields:
            return 0

        # Map Shopware custom field names to ERPNext field names
        # This is configurable via Field Mappings
        from ecommerce_integrations.shopware6.export.utils import get_field_mappings_cached

        mappings = get_field_mappings_cached()
        reverse_mappings = {}

        for erpnext_field, config in mappings.items():
            if config.get('mapping_type') == 'Custom Field':
                shopware_field = config.get('shopware_field')
                if shopware_field:
                    reverse_mappings[shopware_field] = erpnext_field

        updated = 0
        item = frappe.get_doc("Item", item_code)

        for sw_field, value in custom_fields.items():
            erp_field = reverse_mappings.get(sw_field)
            if erp_field and hasattr(item, erp_field):
                current_value = getattr(item, erp_field, None)
                if current_value != value:
                    setattr(item, erp_field, value)
                    updated += 1

        if updated > 0:
            item.save(ignore_permissions=True)
            self.logger.info(f"Updated {updated} custom fields for {item_code}")

        return updated


@frappe.whitelist()
@temp_shopware_session
def sync_property_groups_from_shopware(client, limit: int = 500) -> dict[str, Any]:
    """
    Sync all property groups from Shopware to ERPNext Item Attributes.

    This creates the full property structure in ERPNext without linking
    to specific products.

    Args:
        client: Shopware API client (injected by decorator)
        limit: Maximum number of groups to sync

    Returns:
        dict with sync statistics
    """
    from ecommerce_integrations.shopware6.services.access import require_shopware_admin

    require_shopware_admin()
    importer = PropertyImporter()

    if not importer.is_import_enabled():
        return {
            "success": False,
            "message": "Property import is disabled in Shopware Settings. Enable 'import_properties_from_shopware' to use this feature."
        }

    stats = {
        "groups_synced": 0,
        "options_synced": 0,
        "errors": [],
    }

    try:
        # Fetch all property groups with their options
        page = 1
        all_groups = []

        while True:
            response = client.request_post("search/property-group", {
                "limit": 100,
                "page": page,
                "associations": {
                    "options": {}
                }
            })
            groups = response.get("data", [])

            if not groups:
                break

            all_groups.extend(groups)

            if len(groups) < 100 or len(all_groups) >= limit:
                break

            page += 1

        frappe.logger().info(f"[Property Import] Found {len(all_groups)} property groups in Shopware")

        for group in all_groups[:limit]:
            group_name = group.get("name") or (group.get("translated") or {}).get("name", "")
            if not group_name:
                continue

            options = group.get("options", [])
            option_values = []

            for opt in options:
                opt_name = opt.get("name") or (opt.get("translated") or {}).get("name", "")
                if opt_name:
                    option_values.append(opt_name)

            if option_values:
                for opt_val in option_values:
                    try:
                        importer._ensure_item_attribute(group_name, opt_val)
                        stats["options_synced"] += 1
                    except Exception as e:
                        stats["errors"].append(f"{group_name}/{opt_val}: {str(e)[:100]}")

                stats["groups_synced"] += 1

            # Commit periodically
            if stats["groups_synced"] % 10 == 0:
                frappe.db.commit()

        frappe.db.commit()

        return {
            "success": True,
            "statistics": stats,
            "message": f"Synced {stats['groups_synced']} property groups with {stats['options_synced']} options"
        }

    except Exception as e:
        return {
            "success": False,
            "statistics": stats,
            "message": f"Property sync failed: {e!s}"
        }


@frappe.whitelist()
def import_properties_from_shopware(item_code: str, shopware_id: str | None = None) -> dict[str, Any]:
    """
    Import properties for a specific product from Shopware.

    Args:
        item_code: ERPNext Item code
        shopware_id: Shopware product ID (optional, will lookup if not provided)

    Returns:
        dict with import statistics
    """
    from ecommerce_integrations.shopware6.services.access import require_item_write_permission

    require_item_write_permission(item_code)
    importer = PropertyImporter()

    if not importer.is_import_enabled():
        return {
            "success": False,
            "message": "Property import is disabled in Shopware Settings"
        }

    # Get Shopware ID if not provided
    if not shopware_id:
        from ecommerce_integrations.shopware6.export.utils import get_shopware_document_id
        shopware_id = get_shopware_document_id("Item", item_code)

        if not shopware_id:
            return {
                "success": False,
                "message": f"Item {item_code} is not synced to Shopware"
            }

    client = get_shopware_client()
    stats = importer.import_properties_for_product(client, item_code, shopware_id)

    return {
        "success": len(stats.get("errors", [])) == 0,
        "statistics": stats,
        "message": f"Imported {stats['property_values_created']} properties, {stats['custom_fields_updated']} custom fields"
    }


@frappe.whitelist()
@temp_shopware_session
def batch_import_properties(client, limit: int = 100, dry_run: bool = False) -> dict[str, Any]:
    """
    Batch import properties for all synced products.

    Args:
        client: Shopware API client
        limit: Maximum number of products to process
        dry_run: If True, only analyze what would be imported

    Returns:
        dict with batch statistics
    """
    from ecommerce_integrations.shopware6.services.access import require_shopware_admin

    require_shopware_admin()
    importer = PropertyImporter()

    # Skip the enabled check for dry_run (allows previewing even when disabled)
    if not dry_run and not importer.is_import_enabled():
        return {
            "success": False,
            "message": "Property import is disabled in Shopware Settings"
        }

    stats = {
        "processed": 0,
        "properties_imported": 0,
        "custom_fields_updated": 0,
        "groups_found": 0,
        "would_import": [] if dry_run else None,
        "errors": [],
    }

    # Get all synced items
    ecom_items = frappe.get_all(
        "Ecommerce Item",
        filters={"integration": MODULE_NAME, "has_variants": 0},
        fields=["erpnext_item_code", "integration_item_code"],
        limit=limit
    )

    for ecom_item in ecom_items:
        item_code = ecom_item.erpnext_item_code
        shopware_id = ecom_item.integration_item_code

        try:
            if dry_run:
                # Dry run: just fetch and analyze
                result = importer.analyze_properties_for_product(client, item_code, shopware_id)
                stats["processed"] += 1
                if result.get("properties"):
                    stats["properties_imported"] += len(result.get("properties", []))
                    stats["would_import"].append({
                        "item_code": item_code,
                        "properties": list(result.get("properties", {}).keys())[:5]  # First 5
                    })
            else:
                # Actual import
                result = importer.import_properties_for_product(client, item_code, shopware_id)
                stats["processed"] += 1
                stats["properties_imported"] += result.get("property_values_created", 0)
                stats["custom_fields_updated"] += result.get("custom_fields_updated", 0)
                stats["errors"].extend(result.get("errors", []))

        except Exception as e:
            stats["errors"].append(f"{item_code}: {str(e)[:100]}")
            stats["processed"] += 1

        # Commit periodically (only if not dry_run)
        if not dry_run and stats["processed"] % 20 == 0:
            frappe.db.commit()

    if not dry_run:
        frappe.db.commit()

    return {
        "success": len(stats["errors"]) < stats["processed"] / 2,
        "dry_run": dry_run,
        "statistics": stats,
        "message": f"{'[DRY RUN] ' if dry_run else ''}Processed {stats['processed']} items: {stats['properties_imported']} properties"
    }
