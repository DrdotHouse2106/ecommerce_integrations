"""
Shopware 6 Product Export Module (Compatibility Layer)

This file re-exports functions from the new modular structure under shopware6/export/.
It maintains backwards compatibility for existing code that imports from product_export.

The actual implementation is now in:
- export/product_uploader.py - ShopwareProduct class
- export/product_mapper.py - Field mapping
- export/template_handler.py - Template products
- export/variant_handler.py - Variant products
- export/category_handler.py - Category sync
- export/image_handler.py - Image sync
- export/price_handler.py - Price sync
- export/property_handler.py - Properties and custom fields
- export/utils.py - Utility functions

For new code, import directly from the export module:
    from ecommerce_integrations.shopware6.export import ShopwareProduct
"""

# Re-export everything from the new modules for backwards compatibility
from ecommerce_integrations.shopware6.export import (
    # Main class
    ShopwareProduct,
    # Upload functions
    upload_erpnext_item_to_shopware,
    upload_template_item_to_shopware,
    upload_variant_item_to_shopware,
    sync_all_variants,
    # Categories
    sync_category_hierarchy,
    sync_all_item_categories,
    get_or_create_category,
    sync_item_group_to_shopware,
    # Images
    sync_product_images_to_shopware,
    upload_media_to_shopware,
    # Prices
    sync_product_price,
    sync_bulk_prices,
    update_item_price_in_shopware,
    get_item_price,
    # Mapper
    map_erpnext_item_to_shopware,
    get_tax_id_by_rate,
    get_or_create_manufacturer,
    get_cached_currency_id,
    get_cached_sales_channel_id,
    # Properties
    get_or_create_property_group,
    get_or_create_property_option,
    get_item_properties,
    get_item_custom_fields,
    ensure_shopware_custom_field_set,
    # Utils
    generate_uuid,
    sanitize_filename,
    get_shopware_document_id,
    get_field_mappings,
    # Reconciliation
    sync_all_categories_to_shopware,
    reconcile_erpnext_with_shopware,
    reconcile_all_to_shopware,
    full_reconciliation,
    enqueue_full_reconciliation,
    enqueue_full_reconciliation_with_categories,
    cleanup_orphaned_shopware_categories,
)

# Additional re-exports for bulk_sync.py compatibility
from ecommerce_integrations.shopware6.export.property_handler import (
    get_or_create_variant_option,
)

# Cache management
from ecommerce_integrations.shopware6.base.cache_manager import clear_shopware_cache

import frappe
from frappe import _


# ============================================================================
# Backwards-compatible wrapper functions for external callers
# ============================================================================

@frappe.whitelist()
def sync_item_to_shopware(item_code: str) -> dict:
    """
    Sync a single ERPNext item to Shopware.

    This is a backwards-compatible wrapper for external callers like ai_description/api.py.

    Args:
        item_code: The ERPNext Item code to sync

    Returns:
        dict with success status and message
    """
    try:
        product = ShopwareProduct(item_code=item_code)
        result = product.upload()

        return {
            "success": True,
            "shopware_id": result,
            "message": f"Item {item_code} synced to Shopware"
        }
    except Exception as e:
        frappe.log_error(
            message=f"Failed to sync item {item_code} to Shopware: {str(e)}",
            title="Shopware Sync Error"
        )
        return {
            "success": False,
            "message": str(e)
        }


@frappe.whitelist()
def sync_template_with_variants_to_shopware(template_item_code: str) -> dict:
    """
    Sync a template item with all its variants to Shopware.

    Args:
        template_item_code: The ERPNext template Item code

    Returns:
        dict with success status and synced variants count
    """
    try:
        # First sync the template
        template_result = upload_template_item_to_shopware(template_item_code)

        # Then sync all variants
        variants_synced = sync_all_variants(template_item_code)

        return {
            "success": True,
            "template_id": template_result,
            "variants_synced": variants_synced,
            "message": f"Template {template_item_code} and variants synced"
        }
    except Exception as e:
        frappe.log_error(
            message=f"Failed to sync template {template_item_code}: {str(e)}",
            title="Shopware Template Sync Error"
        )
        return {
            "success": False,
            "message": str(e)
        }


__all__ = [
    # Main class
    "ShopwareProduct",
    # Upload functions
    "upload_erpnext_item_to_shopware",
    "upload_template_item_to_shopware",
    "upload_variant_item_to_shopware",
    "sync_all_variants",
    # Categories
    "sync_category_hierarchy",
    "sync_all_item_categories",
    "get_or_create_category",
    "sync_item_group_to_shopware",
    # Images
    "sync_product_images_to_shopware",
    "upload_media_to_shopware",
    # Prices
    "sync_product_price",
    "sync_bulk_prices",
    "update_item_price_in_shopware",
    "get_item_price",
    # Mapper
    "map_erpnext_item_to_shopware",
    "get_tax_id_by_rate",
    "get_or_create_manufacturer",
    "get_cached_currency_id",
    "get_cached_sales_channel_id",
    # Properties
    "get_or_create_property_group",
    "get_or_create_property_option",
    "get_or_create_variant_option",
    "get_item_properties",
    "get_item_custom_fields",
    "ensure_shopware_custom_field_set",
    # Utils
    "generate_uuid",
    "sanitize_filename",
    "get_shopware_document_id",
    "get_field_mappings",
    # Reconciliation
    "sync_all_categories_to_shopware",
    "reconcile_erpnext_with_shopware",
    "reconcile_all_to_shopware",
    "full_reconciliation",
    "enqueue_full_reconciliation",
    "enqueue_full_reconciliation_with_categories",
    "cleanup_orphaned_shopware_categories",
    # Cache
    "clear_shopware_cache",
    # Backwards-compatible wrappers
    "sync_item_to_shopware",
    "sync_template_with_variants_to_shopware",
]
