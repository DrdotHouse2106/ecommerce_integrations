"""
Shopware 6 Product Export Module

This file re-exports functions from the new modular structure under shopware6/export/.

The actual implementation is now in:
- export/product_uploader.py - ShopwareProductUploader class
- export/product_mapper.py - Field mapping
- export/template_handler.py - Template products
- export/variant_handler.py - Variant products
- export/category_handler.py - Category sync
- export/image_handler.py - Image sync
- export/price_handler.py - Price sync
- export/property_handler.py - Properties and custom fields
- export/utils.py - Utility functions

For new code, import directly from the export module:
    from ecommerce_integrations.shopware6.export import ShopwareProductUploader
"""

# Re-export everything from the new modules
from ecommerce_integrations.shopware6.export import (
    # Main class
    ShopwareProductUploader,
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

    Args:
        item_code: The ERPNext Item code to sync

    Returns:
        dict with success status and message
    """
    from ecommerce_integrations.shopware6.utils import get_logger

    logger = get_logger("sync_item_to_shopware")
    try:
        uploader = ShopwareProductUploader(item_code=item_code)
        result = uploader.upload()

        return {
            "success": True,
            "shopware_id": result,
            "message": f"Item {item_code} synced to Shopware"
        }
    except Exception as e:
        logger.error(f"Failed to sync item {item_code} to Shopware", exception=e)
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
    from ecommerce_integrations.shopware6.utils import get_logger, create_shopware_log
    from ecommerce_integrations.shopware6.connection import get_shopware_client

    logger = get_logger("sync_template_with_variants")
    try:
        # Get the template item document
        template_item = frappe.get_doc("Item", template_item_code)
        
        # Get Shopware client
        client = get_shopware_client()
        
        # First sync the template
        template_result = upload_template_item_to_shopware(client, template_item)

        # Then sync all variants
        variants_synced = sync_all_variants(client, template_item_code)

        return {
            "success": True,
            "template_id": template_result,
            "variants_synced": variants_synced,
            "message": f"Template {template_item_code} and variants synced"
        }
    except Exception as e:
        logger.error(f"Failed to sync template {template_item_code}", exception=e)
        create_shopware_log(
            status="Error",
            method="sync_template_with_variants",
            message=f"Failed to sync template {template_item_code}",
            exception=str(e),
            make_new=True
        )
        return {
            "success": False,
            "message": str(e)
        }


__all__ = [
    # Main class
    "ShopwareProductUploader",
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
