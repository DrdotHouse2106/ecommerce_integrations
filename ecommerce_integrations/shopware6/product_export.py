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
import frappe
from frappe import _

# Cache management
from ecommerce_integrations.shopware6.base.cache_manager import clear_shopware_cache
from ecommerce_integrations.shopware6.constants import ROOT_ITEM_GROUPS
from ecommerce_integrations.shopware6.export import (
    delete_category_from_shopware,
    ensure_shopware_custom_field_set,
    # Utils
    generate_uuid,
    get_cached_currency_id,
    get_cached_sales_channel_id,
    get_field_mappings,
    get_item_custom_fields,
    get_item_price,
    get_item_properties,
    get_or_create_category,
    get_or_create_manufacturer,
    # Properties
    get_or_create_property_group,
    get_or_create_property_option,
    get_shopware_document_id,
    get_tax_id_by_rate,
    # Mapper
    map_erpnext_item_to_shopware,
    rename_category_in_shopware,
    sanitize_filename,
    # Categories
    sync_all_item_categories,
    sync_all_variants,
    sync_bulk_prices,
    sync_category_hierarchy,
    sync_item_group_to_shopware,
    sync_product_images_to_shopware,
    # Prices
    sync_product_price,
    update_item_price_in_shopware,
    # Upload helpers
    upload_media_to_shopware,
    upload_template_item_to_shopware,
    upload_variant_item_to_shopware,
)

# Additional re-exports for bulk_sync.py compatibility
from ecommerce_integrations.shopware6.export.property_handler import (
    get_or_create_variant_option,
)
from ecommerce_integrations.shopware6.utils import require_item_write_permission

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

    require_item_write_permission(item_code)
    logger = get_logger("sync_item_to_shopware")
    try:
        # Route through the delta product-sync engine (same canonical → hash →
        # diff → push pipeline as the cron / per-save dispatch), not the
        # retired single-item uploader.
        from ecommerce_integrations.product_sync.constants import BACKEND_SHOPWARE
        from ecommerce_integrations.product_sync.tasks import dispatch_item_change

        res = (dispatch_item_change(item_code, BACKEND_SHOPWARE) or {}).get(BACKEND_SHOPWARE)
        status = getattr(res, "status", None)
        if status and status != "ok":
            return {"success": False, "message": f"sync status: {status}"}
        return {
            "success": True,
            "message": f"Item {item_code} synced to Shopware",
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
    from ecommerce_integrations.shopware6.connection import get_shopware_client
    from ecommerce_integrations.shopware6.utils import create_shopware_log, get_logger

    require_item_write_permission(template_item_code)
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


@frappe.whitelist()
def sync_category_to_shopware(item_group_name: str) -> dict:
    """
    Sync a single ERPNext Item Group (category) to Shopware.

    Args:
        item_group_name: The ERPNext Item Group name to sync

    Returns:
        dict with success status and message
    """
    from ecommerce_integrations.shopware6.connection import get_shopware_client
    from ecommerce_integrations.shopware6.utils import get_logger
    frappe.only_for("System Manager")

    logger = get_logger("sync_category_to_shopware")

    # Skip root categories
    root_to_skip = ROOT_ITEM_GROUPS
    if item_group_name in root_to_skip:
        return {
            "success": False,
            "message": _("Root-Kategorien können nicht synchronisiert werden")
        }

    try:
        client = get_shopware_client()
        result = sync_item_group_to_shopware(client, item_group_name)

        if result:
            return {
                "success": True,
                "message": _("Kategorie {0} mit Shopware synchronisiert").format(item_group_name)
            }
        else:
            return {
                "success": False,
                "message": _("Kategorie {0} konnte nicht synchronisiert werden").format(item_group_name)
            }
    except Exception as e:
        logger.error(f"Failed to sync category {item_group_name} to Shopware", exception=e)
        return {
            "success": False,
            "message": str(e)
        }


__all__ = [
    # Cache
    "clear_shopware_cache",
    "delete_category_from_shopware",
    "ensure_shopware_custom_field_set",
    # Utils
    "generate_uuid",
    "get_cached_currency_id",
    "get_cached_sales_channel_id",
    "get_field_mappings",
    "get_item_custom_fields",
    "get_item_price",
    "get_item_properties",
    "get_or_create_category",
    "get_or_create_manufacturer",
    # Properties
    "get_or_create_property_group",
    "get_or_create_property_option",
    "get_or_create_variant_option",
    "get_shopware_document_id",
    "get_tax_id_by_rate",
    # Mapper
    "map_erpnext_item_to_shopware",
    "rename_category_in_shopware",
    "sanitize_filename",
    "sync_all_item_categories",
    "sync_all_variants",
    "sync_bulk_prices",
    # Categories
    "sync_category_hierarchy",
    "sync_category_to_shopware",
    "sync_item_group_to_shopware",
    # Backwards-compatible wrappers
    "sync_item_to_shopware",
    # Images
    "sync_product_images_to_shopware",
    # Prices
    "sync_product_price",
    "sync_template_with_variants_to_shopware",
    "update_item_price_in_shopware",
    # Upload helpers
    "upload_media_to_shopware",
    "upload_template_item_to_shopware",
    "upload_variant_item_to_shopware",
]
