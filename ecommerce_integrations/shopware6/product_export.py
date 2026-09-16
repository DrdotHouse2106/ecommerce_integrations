"""
Shopware 6 Product Export Module

This file re-exports functions from the new modular structure under shopware6/export/,
plus the whitelisted API entry points for the Item form's manual sync buttons.

The actual implementation is now in:
- export/product_mapper.py - Shared lookup/get-or-create helpers
- export/category_handler.py - Category sync
- export/price_handler.py - Pricing helpers for the force-resync admin tools
- export/property_handler.py - Properties and custom fields
- export/utils.py - Utility functions

Product pushes themselves go through the delta product-sync engine
(``product_sync/``, see ``sync_item_to_shopware`` below) — there is no
standalone product uploader in this module anymore.
"""

# Re-export everything from the new modules
import frappe
from frappe import _

# Cache management
from ecommerce_integrations.shopware6.base.cache_manager import clear_shopware_cache
from ecommerce_integrations.shopware6.constants import ROOT_ITEM_GROUPS
from ecommerce_integrations.shopware6.export import (
    delete_category_from_shopware,
    # Utils
    generate_uuid,
    get_cached_currency_id,
    get_cached_sales_channel_id,
    get_field_mappings,
    get_item_price,
    get_or_create_category,
    get_or_create_manufacturer,
    # Properties
    get_or_create_property_group,
    get_or_create_property_option,
    get_shopware_document_id,
    get_tax_id_by_rate,
    rename_category_in_shopware,
    sanitize_filename,
    # Categories
    sync_all_item_categories,
    sync_category_hierarchy,
    sync_item_group_to_shopware,
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
def sync_item_to_shopware(item_code: str, include_variants: bool = False) -> dict:
    """
    Force a full resync of a single ERPNext item — and optionally every
    one of its variants — to Shopware, bypassing the normal delta-hash
    gate. This is the manual "Komplett-Resync" button's entry point, so
    it always pushes even when nothing changed (unlike the doc-event
    dispatch, which stays delta-gated).

    Args:
        item_code: The ERPNext Item code to sync
        include_variants: Also force-resync every variant of this
            template in the same call. Meaningless (silently ignored)
            when item_code isn't a template.

    Returns:
        dict with success status, message, and (when include_variants
        is set) a variants_synced count
    """
    from frappe.utils import cint

    from ecommerce_integrations.shopware6.utils import get_logger

    require_item_write_permission(item_code)
    logger = get_logger("sync_item_to_shopware")
    try:
        # Route through the delta product-sync engine (same canonical → hash →
        # diff → push pipeline as the cron / per-save dispatch), not the
        # retired template/variant uploader. force=True bypasses the delta
        # gate since this is an explicit manual "resync now" action.
        from ecommerce_integrations.product_sync.constants import BACKEND_SHOPWARE
        from ecommerce_integrations.product_sync.tasks import dispatch_item_change

        include_variants = bool(cint(include_variants))
        res = (
            dispatch_item_change(
                item_code,
                BACKEND_SHOPWARE,
                force=True,
                include_variants=include_variants,
            )
            or {}
        ).get(BACKEND_SHOPWARE)
        status = getattr(res, "status", None)
        if status and status != "ok":
            return {"success": False, "message": f"sync status: {status}"}

        result = {
            "success": True,
            "message": f"Item {item_code} synced to Shopware",
        }
        if include_variants and res is not None:
            pushed = (res.created or 0) + (res.updated or 0)
            result["variants_synced"] = max(pushed - 1, 0)
        return result
    except Exception as e:
        logger.error(f"Failed to sync item {item_code} to Shopware", exception=e)
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
    # Utils
    "generate_uuid",
    "get_cached_currency_id",
    "get_cached_sales_channel_id",
    "get_field_mappings",
    "get_item_price",
    "get_or_create_category",
    "get_or_create_manufacturer",
    # Properties
    "get_or_create_property_group",
    "get_or_create_property_option",
    "get_or_create_variant_option",
    "get_shopware_document_id",
    "get_tax_id_by_rate",
    "rename_category_in_shopware",
    "sanitize_filename",
    "sync_all_item_categories",
    # Categories
    "sync_category_hierarchy",
    "sync_category_to_shopware",
    "sync_item_group_to_shopware",
    # Backwards-compatible wrappers
    "sync_item_to_shopware",
]
