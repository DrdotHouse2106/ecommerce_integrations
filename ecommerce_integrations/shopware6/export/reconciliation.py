"""
Shopware 6 Reconciliation Module

Handles full reconciliation between ERPNext and Shopware:
- Category sync (all categories under root)
- Product comparison and sync
- Batch processing for large datasets
- Background job support

Usage:
    # Quick reconciliation
    from ecommerce_integrations.shopware6.export.reconciliation import (
        full_reconciliation,
        enqueue_full_reconciliation,
    )

    # Run synchronously (small datasets)
    result = full_reconciliation(limit=100, sync_images=True)

    # Run in background (large datasets)
    enqueue_full_reconciliation(batch_size=50, sync_images=True)
"""

from typing import Any
from frappe.utils import flt, now, cint, create_batch

import frappe

from ecommerce_integrations.shopware6.connection import (
    temp_shopware_session,
    get_shopware_client,
)
from ecommerce_integrations.shopware6.constants import (
    MODULE_NAME,
    ROOT_ITEM_GROUPS,
    SETTING_DOCTYPE,
    ITEM_SELLING_RATE_FIELD,
)
from ecommerce_integrations.shopware6.utils import create_shopware_log, update_shopware_log, get_logger

# Module-level logger for generic logging
_logger = get_logger("reconciliation")


# =============================================================================
# CATEGORY RECONCILIATION
# =============================================================================

@frappe.whitelist()
@temp_shopware_session
def sync_all_categories_to_shopware(
    client,
    root_category: str = None,
    skip_root: bool = False,
    sync_empty_categories: bool = True,
    dry_run: bool = False,
    use_bulk_sync: bool = True,
    sync_images: bool = True
) -> dict[str, Any]:
    """
    Sync ALL Item Groups under a root category to Shopware.

    Processes in tree order (parent before children) using lft ordering.
    Uses bulk Sync API for massive performance improvement (2 API calls vs hundreds).

    Args:
        client: Shopware API client (injected by decorator)
        root_category: Root category (defaults to setting)
        skip_root: If True, don't sync the root category itself
        sync_empty_categories: If True, sync categories even without products
        dry_run: If True, only report what would be synced
        use_bulk_sync: If True, use bulk Sync API (default, much faster)
        sync_images: If True, sync category images after bulk sync

    Returns:
        dict: Sync results with statistics
    """
    from ecommerce_integrations.shopware6.export.category_handler import (
        sync_category_hierarchy,
        bulk_sync_categories,
        bulk_sync_category_images,
    )

    # Parse string parameters from frontend
    skip_root = _parse_bool(skip_root)
    sync_empty_categories = _parse_bool(sync_empty_categories)
    dry_run = _parse_bool(dry_run)
    use_bulk_sync = _parse_bool(use_bulk_sync)
    sync_images = _parse_bool(sync_images)

    setting = frappe.get_cached_doc(SETTING_DOCTYPE)
    if not setting.is_enabled():
        return {"success": False, "message": "Shopware integration is not enabled"}

    # Use setting default if not provided
    if not root_category:
        root_category = getattr(setting, 'category_sync_root', 'Products') or 'Products'

    # Get root category info for nested set query
    root_info = frappe.db.get_value(
        "Item Group", root_category, ["lft", "rgt", "name"], as_dict=True
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

    # Get categories in tree order (parent before children)
    categories = frappe.get_all(
        "Item Group",
        filters=filters,
        fields=["name", "parent_item_group", "is_group"],
        order_by="lft asc"
    )

    # Filter empty categories if requested
    # Note: Include disabled items - they still belong to categories (will be synced as inactive)
    if not sync_empty_categories:
        categories_with_products = set(frappe.get_all(
            "Item", pluck="item_group"
        ))
        categories = [c for c in categories if c.name in categories_with_products]

    stats = {"synced": 0, "skipped": 0, "errors": [], "total": len(categories)}

    _logger.info(
        f"Category Sync: Processing {len(categories)} categories from '{root_category}'"
    )

    if dry_run:
        stats["synced"] = len(categories)
        return {
            "success": True,
            "dry_run": True,
            "statistics": stats,
            "message": f"Would sync {stats['synced']}/{stats['total']} categories (DRY RUN)"
        }

    # =========================================================================
    # BULK SYNC MODE (default, much faster)
    # =========================================================================
    if use_bulk_sync:
        _logger.info("Using BULK sync mode for categories")

        # Convert to list of dicts for bulk_sync_categories
        item_groups = [{"name": c.name, "parent_item_group": c.parent_item_group} for c in categories]

        # Execute bulk sync (2 API calls instead of hundreds)
        bulk_result = bulk_sync_categories(client, item_groups)

        if bulk_result.get("success"):
            bulk_stats = bulk_result.get("stats", {})
            stats["synced"] = bulk_stats.get("created", 0) + bulk_stats.get("updated", 0)
            stats["skipped"] = bulk_stats.get("skipped", 0)
            stats["errors"] = bulk_stats.get("errors", [])

            # Sync images separately (still sequential for now)
            if sync_images:
                _logger.info("Syncing category images...")
                id_map = bulk_result.get("id_map", {})
                image_stats = bulk_sync_category_images(client, item_groups, id_map)
                _logger.info(f"Category images: {image_stats['synced']} synced, "
                             f"{image_stats['skipped']} skipped, {image_stats['errors']} errors")
        else:
            stats["errors"].append({"category": "bulk_sync", "error": bulk_result.get("error", "Unknown")})
            _logger.error(f"Bulk category sync failed: {bulk_result.get('error')}")

    # =========================================================================
    # SEQUENTIAL SYNC MODE (fallback, slower)
    # =========================================================================
    else:
        _logger.info("Using SEQUENTIAL sync mode for categories (slower)")

        for cat in categories:
            try:
                category_id = sync_category_hierarchy(
                    client=client, item_group_name=cat.name
                )
                if category_id:
                    stats["synced"] += 1
                else:
                    stats["skipped"] += 1
            except Exception as e:
                stats["errors"].append({"category": cat.name, "error": str(e)[:200]})
                logger = get_logger("sync_all_categories_to_shopware")
                logger.error(f"Category sync failed: {cat.name}", exception=e, persist=True)
                # Check for DB connection error and reconnect
                if "InterfaceError" in str(type(e).__name__) or "(0, '')" in str(e):
                    try:
                        frappe.connect()
                        _logger.info(f"Category sync: Reconnected after DB error for {cat.name}")
                    except Exception:
                        pass

            # Commit periodically with error handling
            if stats["synced"] % 20 == 0:
                try:
                    frappe.db.commit()
                except Exception as commit_error:
                    _logger.warning(f"Category sync: Commit failed, trying reconnect: {commit_error}")
                    try:
                        frappe.connect()
                        frappe.db.commit()
                    except Exception:
                        pass

    try:
        frappe.db.commit()
    except Exception:
        try:
            frappe.connect()
            frappe.db.commit()
        except Exception:
            pass

    create_shopware_log(
        status="Success" if not stats["errors"] else "Partial",
        message=f"Category sync: {stats['synced']}/{stats['total']} synced, {len(stats['errors'])} errors",
        method="sync_all_categories_to_shopware"
    )

    return {
        "success": True,
        "dry_run": dry_run,
        "statistics": stats,
        "message": f"Synced {stats['synced']}/{stats['total']} categories" + (" (BULK)" if use_bulk_sync else "")
    }


# =============================================================================
# PRODUCT RECONCILIATION
# =============================================================================

def get_shopware_products_batch(
    client,
    product_ids: list[str],
    include_categories: bool = True
) -> dict[str, dict[str, Any]]:
    """
    Fetch multiple products from Shopware in a single API call.

    Args:
        client: Shopware API client
        product_ids: List of Shopware product UUIDs
        include_categories: Include category associations

    Returns:
        Dict mapping product ID to product data
    """
    if not product_ids:
        return {}

    try:
        search_payload = {
            "limit": len(product_ids),
            "ids": product_ids,
            "associations": {
                "media": {},
                "cover": {"associations": {"media": {}}}
            }
        }
        if include_categories:
            search_payload["associations"]["categories"] = {}

        response = client.request_post("search/product", search_payload)
        return {p.get("id"): p for p in response.get("data", []) if p.get("id")}

    except Exception as e:
        logger = get_logger("_fetch_shopware_products_batch")
        logger.error("Batch product fetch failed", exception=e, persist=True)
        return {}


def compare_item_with_shopware(
    erpnext_item,
    shopware_data: dict[str, Any],
    compare_categories: bool = True
) -> dict[str, Any]:
    """
    Compare ERPNext Item with Shopware product data.

    Returns:
        dict with needs_sync (bool), differences (list), details (dict)
    """
    differences = []
    details = {}

    # Name comparison
    if erpnext_item.item_name != shopware_data.get("name", ""):
        differences.append("name")
        details["name"] = {"erpnext": erpnext_item.item_name, "shopware": shopware_data.get("name")}

    # Description (priority: ai > web > description > item_name)
    erpnext_desc = (
        getattr(erpnext_item, 'ai_long_description', None) or
        getattr(erpnext_item, 'web_long_description', None) or
        erpnext_item.description or
        erpnext_item.item_name
    )
    shopware_desc = shopware_data.get("description", "") or ""
    if erpnext_desc and shopware_desc and erpnext_desc.strip() != shopware_desc.strip():
        differences.append("description")

    # Active status
    erpnext_active = not erpnext_item.disabled
    if erpnext_active != shopware_data.get("active", False):
        differences.append("active")
        details["active"] = {"erpnext": erpnext_active, "shopware": shopware_data.get("active")}

    # Price (with tolerance)
    erpnext_price = flt(erpnext_item.get(ITEM_SELLING_RATE_FIELD) or 0)
    shopware_prices = shopware_data.get("price", [])
    if shopware_prices and erpnext_price > 0:
        shopware_net = flt(shopware_prices[0].get("net", 0))
        if abs(erpnext_price - shopware_net) > 0.01:
            differences.append("price")
            details["price"] = {"erpnext": erpnext_price, "shopware": shopware_net}

    # Weight
    erpnext_weight = flt(erpnext_item.weight_per_unit or 0)
    shopware_weight = flt(shopware_data.get("weight", 0))
    if abs(erpnext_weight - shopware_weight) > 0.001:
        differences.append("weight")

    # Dimensions (ERPNext: cm, Shopware: mm)
    for dim in ["height", "width", "length"]:
        erpnext_val = flt(getattr(erpnext_item, f'item_{dim}', 0) or 0)
        shopware_val = flt(shopware_data.get(dim, 0)) / 10 if shopware_data.get(dim) else 0
        if abs(erpnext_val - shopware_val) > 0.01:
            if "dimensions" not in differences:
                differences.append("dimensions")
            break

    # Product number
    if erpnext_item.item_code != shopware_data.get("productNumber", ""):
        differences.append("productNumber")

    # Categories
    if compare_categories:
        from ecommerce_integrations.shopware6.export.category_handler import get_all_item_categories
        erpnext_cats = sorted(get_all_item_categories(erpnext_item.item_code) or [])
        shopware_cats = sorted([
            c.get("name") or (c.get("translated") or {}).get("name", "")
            for c in shopware_data.get("categories") or []
        ])
        if erpnext_cats != shopware_cats:
            differences.append("categories")
            details["categories"] = {"erpnext": erpnext_cats, "shopware": shopware_cats}

    # Images
    erpnext_has_image = bool(erpnext_item.image)
    shopware_has_image = bool(shopware_data.get("media")) or bool(
        (shopware_data.get("cover") or {}).get("media")
    )
    if erpnext_has_image != shopware_has_image:
        differences.append("image")

    # Custom Fields (from PRODUCT_CUSTOM_FIELDS_MAP + ecommerce_properties child table)
    cf_diffs = _compare_custom_fields(erpnext_item, shopware_data)
    if cf_diffs:
        differences.append("customFields")
        details["customFields"] = cf_diffs

    return {
        "needs_sync": len(differences) > 0,
        "differences": differences,
        "details": details
    }


def _compare_custom_fields(erpnext_item, shopware_data: dict[str, Any]) -> dict | None:
    """Compare ERPNext custom fields with Shopware customFields.

    Builds the expected custom fields dict the same way the uploader does
    (PRODUCT_CUSTOM_FIELDS_MAP + ecommerce_properties child table) and compares
    with what Shopware currently has.

    Returns:
        Dict with differing fields or None if in sync.
    """
    from frappe.utils import cstr
    from ecommerce_integrations.shopware6.constants import PRODUCT_CUSTOM_FIELDS_MAP
    from ecommerce_integrations.property_utils import (
        get_ecommerce_properties,
        shopware_custom_field_name,
        coerce_custom_field_value,
    )

    expected = {}

    for erpnext_field, shopware_field in PRODUCT_CUSTOM_FIELDS_MAP.items():
        value = getattr(erpnext_item, erpnext_field, None)
        if value:
            expected[shopware_field] = cstr(value).strip()

    for prop in get_ecommerce_properties(erpnext_item):
        if prop.property_type == "Custom Field" and prop.property_value:
            field_name = shopware_custom_field_name(prop.property_name)
            expected[field_name] = coerce_custom_field_value(cstr(prop.property_value).strip())

    if not expected:
        return None

    # Get current Shopware custom fields
    shopware_cf = shopware_data.get("customFields") or {}

    # Compare only fields we manage (don't flag Shopware-only fields)
    diffs = {}
    for field, erpnext_val in expected.items():
        shopware_val = shopware_cf.get(field)
        # Normalize for comparison: treat None and "" as equivalent
        sw_normalized = cstr(shopware_val).strip() if shopware_val is not None else ""
        erp_normalized = cstr(erpnext_val).strip() if not isinstance(erpnext_val, bool) else erpnext_val

        if isinstance(erpnext_val, bool):
            # Boolean comparison
            if shopware_val != erpnext_val:
                diffs[field] = {"erpnext": erpnext_val, "shopware": shopware_val}
        elif sw_normalized != erp_normalized:
            diffs[field] = {"erpnext": erpnext_val, "shopware": shopware_val}

    return diffs if diffs else None


# DEPRECATED
@temp_shopware_session
def reconcile_erpnext_with_shopware(
    client,
    limit: int = 100,
    dry_run: bool = False,
    sync_images: bool = False,
    include_unlinked: bool = False,
    compare_categories: bool = True
) -> dict[str, Any]:
    """
    DEPRECATED: Use sync_manager.enqueue_full_reconciliation_no_brainer() instead.
    This function uses single-item uploads which are much slower than the batch approach.

    Compare all ERPNext items with Shopware and sync differences.

    Args:
        client: Shopware API client
        limit: Maximum items to process
        dry_run: Only report differences without syncing
        sync_images: Also sync images for changed items
        include_unlinked: Also sync items not yet in Shopware
        compare_categories: Compare and sync categories

    Returns:
        Reconciliation results with statistics
    """
    return {
        "success": False,
        "message": "Deprecated. Use sync_manager.enqueue_full_reconciliation_no_brainer() instead.",
    }
    from ecommerce_integrations.shopware6.export.product_uploader import upload_erpnext_item_to_shopware

    # Parse parameters
    dry_run = _parse_bool(dry_run)
    sync_images = _parse_bool(sync_images)
    include_unlinked = _parse_bool(include_unlinked)
    compare_categories = _parse_bool(compare_categories)

    setting = frappe.get_cached_doc(SETTING_DOCTYPE)
    if not setting.is_enabled():
        return {"success": False, "message": "Shopware integration is not enabled"}

    stats = {
        "total_checked": 0, "in_sync": 0, "out_of_sync": 0,
        "synced": 0, "sync_failed": 0, "not_in_shopware": 0,
        "newly_synced": 0, "errors": []
    }
    out_of_sync_items = []
    synced_items = []

    # Get linked items
    ecom_items = frappe.get_all(
        "Ecommerce Item",
        filters={"integration": MODULE_NAME},
        fields=["erpnext_item_code", "integration_item_code", "has_variants"],
        limit=cint(limit)
    )

    _logger.info(f"Reconciliation: Checking {len(ecom_items)} linked items")

    # Process in batches - get batch size from settings
    batch_size = cint(getattr(setting, 'product_batch_size', 50)) or 50
    for batch_start in range(0, len(ecom_items), batch_size):
        # Ensure database connection is active before each batch
        try:
            frappe.db.sql("SELECT 1")
        except Exception:
            try:
                frappe.connect()
                _logger.info(f"Reconciliation: Reconnected to database at batch {batch_start // batch_size + 1}")
            except Exception as reconnect_error:
                _logger.error(f"Reconciliation: Failed to reconnect: {reconnect_error}")
                continue

        batch = ecom_items[batch_start:batch_start + batch_size]
        shopware_ids = [item.integration_item_code for item in batch]
        shopware_products = get_shopware_products_batch(client, shopware_ids, compare_categories)

        for ecom_item in batch:
            stats["total_checked"] += 1
            item_code = ecom_item.erpnext_item_code
            shopware_id = ecom_item.integration_item_code

            try:
                erpnext_item = frappe.get_doc("Item", item_code)
                shopware_data = shopware_products.get(shopware_id)

                if not shopware_data:
                    stats["not_in_shopware"] += 1
                    stats["errors"].append({
                        "item": item_code, "error": "Not found in Shopware"
                    })
                    continue

                comparison = compare_item_with_shopware(
                    erpnext_item, shopware_data, compare_categories
                )

                if comparison["needs_sync"]:
                    stats["out_of_sync"] += 1
                    out_of_sync_items.append({
                        "item_code": item_code,
                        "differences": comparison["differences"]
                    })

                    if not dry_run:
                        try:
                            # Temporarily disable image sync unless requested
                            orig_img = getattr(setting, 'sync_images_to_shopware', True)
                            if not sync_images:
                                setting.sync_images_to_shopware = False

                            upload_erpnext_item_to_shopware(erpnext_item.item_code)
                            setting.sync_images_to_shopware = orig_img

                            stats["synced"] += 1
                            synced_items.append({
                                "item_code": item_code,
                                "differences": comparison["differences"]
                            })
                        except Exception as e:
                            stats["sync_failed"] += 1
                            stats["errors"].append({"item": item_code, "error": str(e)[:200]})
                else:
                    stats["in_sync"] += 1

            except Exception as e:
                stats["errors"].append({"item": item_code, "error": str(e)[:200]})
                # Check if this is a database connection error and try to reconnect
                if "InterfaceError" in str(type(e).__name__) or "(0, '')" in str(e):
                    try:
                        frappe.connect()
                        _logger.info(f"Reconciliation: Reconnected after DB error for {item_code}")
                    except Exception:
                        pass

        # Commit periodically with error handling
        try:
            if stats["total_checked"] % 20 == 0:
                frappe.db.commit()
        except Exception as commit_error:
            _logger.warning(f"Reconciliation: Commit failed, trying reconnect: {commit_error}")
            try:
                frappe.connect()
                frappe.db.commit()
            except Exception:
                pass

    # Optionally sync unlinked items
    if include_unlinked and not dry_run:
        synced_codes = [e.erpnext_item_code for e in ecom_items]
        # Include disabled items - they will be synced with active=false
        unlinked = frappe.get_all(
            "Item",
            filters={
                "has_variants": 0,
                "variant_of": ["is", "not set"],
                "name": ["not in", synced_codes] if synced_codes else ["is", "set"]
            },
            pluck="name",
            limit=cint(limit) - stats["total_checked"]
        )
        for item_code in unlinked:
            try:
                upload_erpnext_item_to_shopware(item_code)
                stats["newly_synced"] += 1
            except Exception as e:
                stats["errors"].append({"item": item_code, "error": str(e)[:150]})

    frappe.db.commit()

    result = {
        "success": True,
        "dry_run": dry_run,
        "statistics": stats,
        "message": f"Checked {stats['total_checked']} items: {stats['in_sync']} in sync, {stats['out_of_sync']} out of sync"
    }

    if dry_run:
        result["out_of_sync_items"] = out_of_sync_items[:50]
        result["message"] += " (DRY RUN)"
    else:
        result["synced_items"] = synced_items[:50]
        result["message"] += f", {stats['synced']} synced"

    if stats["errors"]:
        result["errors"] = stats["errors"][:20]

    create_shopware_log(
        status="Success",
        message=result["message"],
        method="reconcile_erpnext_with_shopware"
    )

    return result


@frappe.whitelist()
def reconcile_all_to_shopware(
    limit: int = 100,
    dry_run: bool = False,
    sync_images: bool = False,
    compare_categories: bool = True
) -> dict[str, Any]:
    """
    Convenience wrapper for reconcile_erpnext_with_shopware.

    Can be called from frontend without client parameter.
    """
    return reconcile_erpnext_with_shopware(
        limit=cint(limit),
        dry_run=_parse_bool(dry_run),
        sync_images=_parse_bool(sync_images),
        include_unlinked=False,
        compare_categories=_parse_bool(compare_categories)
    )


# =============================================================================
# FULL RECONCILIATION (Categories + Products)
# =============================================================================

# DEPRECATED
def full_reconciliation(
    limit: int = 500,
    dry_run: bool = False,
    sync_images: bool = False,
    include_unlinked: bool = False,
    category_root: str = None,
    skip_root_category: bool = False,
    sync_empty_categories: bool = True,
    cleanup_orphaned_categories: bool = False,
    cleanup_orphaned_variants: bool = True,
    sync_surcharge: bool = True,
    skip_category_sync: bool = None
) -> dict[str, Any]:
    """
    DEPRECATED: Use sync_manager.enqueue_full_reconciliation_no_brainer() instead.
    This function uses single-item uploads which are much slower than the batch approach.

    Full reconciliation: Categories first, then Products, then Variant Cleanup.

    Args:
        limit: Maximum products to process
        dry_run: Only report differences without syncing
        sync_images: Also sync images for changed items
        include_unlinked: Also sync products not yet in Shopware
        category_root: Root category for sync
        skip_root_category: Skip the root category itself
        sync_empty_categories: Sync categories even without products
        cleanup_orphaned_categories: Delete Shopware categories not in ERPNext
        cleanup_orphaned_variants: Delete Shopware variants not in ERPNext
        sync_surcharge: Sync Surcharge properties for items with is_sales_item=0
        skip_category_sync: Skip category sync phase (defaults to setting if None)

    Returns:
        Combined results from category and product sync
    """
    return {
        "success": False,
        "message": "Deprecated. Use sync_manager.enqueue_full_reconciliation_no_brainer() instead.",
    }
    # Parse parameters
    dry_run = _parse_bool(dry_run)
    sync_images = _parse_bool(sync_images)
    include_unlinked = _parse_bool(include_unlinked)
    skip_root_category = _parse_bool(skip_root_category)
    sync_empty_categories = _parse_bool(sync_empty_categories)
    cleanup_orphaned_categories = _parse_bool(cleanup_orphaned_categories)
    cleanup_orphaned_variants = _parse_bool(cleanup_orphaned_variants)
    sync_surcharge = _parse_bool(sync_surcharge)

    setting = frappe.get_cached_doc(SETTING_DOCTYPE)
    
    # If skip_category_sync not explicitly set, use setting value
    if skip_category_sync is None:
        skip_category_sync = getattr(setting, 'skip_category_sync_on_full_reconciliation', False)
    else:
        skip_category_sync = _parse_bool(skip_category_sync)
    
    if not category_root:
        category_root = getattr(setting, 'category_sync_root', 'Products') or 'Products'

    results = {
        "success": True,
        "dry_run": dry_run,
        "category_sync": None if skip_category_sync else "skipped",
        "product_sync": None,
        "category_cleanup": None,
        "variant_cleanup": None,
        "surcharge_sync": None
    }

    _logger.info(
        f"Full Reconciliation: Starting (categories: {category_root if not skip_category_sync else 'SKIPPED'}, products: {limit})"
    )

    def _ensure_db_connection():
        """Ensure database connection is active before each phase."""
        try:
            frappe.db.sql("SELECT 1")
        except Exception:
            try:
                frappe.connect()
                _logger.info("Full Reconciliation: Reconnected to database")
            except Exception as e:
                _logger.warning(f"Full Reconciliation: Reconnect attempt failed: {e}")

    # Phase 0: Sync Surcharge properties (before product sync)
    if sync_surcharge and not dry_run:
        from ecommerce_integrations.shopware6.export.property_handler import sync_surcharge_properties_batch
        _ensure_db_connection()
        _notify_user("Phase 0/5: Syncing Surcharge properties...", "blue")
        try:
            results["surcharge_sync"] = sync_surcharge_properties_batch()
        except Exception as e:
            _logger.error(f"Full Reconciliation: Surcharge sync failed: {e}")
            results["surcharge_sync"] = {"error": str(e)[:200]}
            _ensure_db_connection()

    # Phase 1: Sync categories
    if not skip_category_sync:
        _ensure_db_connection()
        _notify_user("Phase 1/5: Syncing categories...", "blue")
        try:
            results["category_sync"] = sync_all_categories_to_shopware(
                root_category=category_root,
                skip_root=skip_root_category,
                sync_empty_categories=sync_empty_categories,
                dry_run=dry_run
            )
        except Exception as e:
            _logger.error(f"Full Reconciliation: Category sync failed: {e}")
            results["category_sync"] = {"success": False, "error": str(e)[:200], "statistics": {}}
            _ensure_db_connection()
    else:
        _logger.info("Full Reconciliation: Skipping category sync (skip_category_sync=True)")
        _notify_user("Phase 1/5: Skipping categories (disabled in settings)...", "orange")
        results["category_sync"] = {"success": True, "skipped": True, "message": "Category sync skipped per settings"}

    # Phase 2: Sync products
    _ensure_db_connection()
    _notify_user("Phase 2/5: Syncing products...", "blue")
    try:
        results["product_sync"] = reconcile_erpnext_with_shopware(
            limit=cint(limit),
            dry_run=dry_run,
            sync_images=sync_images,
            include_unlinked=include_unlinked,
            compare_categories=True
        )
    except Exception as e:
        _logger.error(f"Full Reconciliation: Product sync failed: {e}")
        results["product_sync"] = {"success": False, "error": str(e)[:200], "statistics": {}}
        _ensure_db_connection()

    # Phase 3: Cleanup orphaned variants
    if cleanup_orphaned_variants and not dry_run:
        _ensure_db_connection()
        _notify_user("Phase 3/5: Cleaning up orphaned variants...", "blue")
        try:
            results["variant_cleanup"] = _cleanup_all_orphaned_variants()
        except Exception as e:
            _logger.error(f"Full Reconciliation: Variant cleanup failed: {e}")
            results["variant_cleanup"] = {"error": str(e)[:200]}
            _ensure_db_connection()

    # Phase 4: Cleanup orphaned categories
    if cleanup_orphaned_categories:
        _ensure_db_connection()
        _notify_user("Phase 4/5: Cleaning up orphaned categories...", "blue")
        try:
            results["category_cleanup"] = cleanup_orphaned_shopware_categories(
                root_category=category_root,
                dry_run=dry_run
            )
        except Exception as e:
            _logger.error(f"Full Reconciliation: Category cleanup failed: {e}")
            results["category_cleanup"] = {"success": False, "error": str(e)[:200], "statistics": {}}
            _ensure_db_connection()

    # Build summary message
    cat_stats = (results.get("category_sync") or {}).get("statistics", {})
    prod_stats = (results.get("product_sync") or {}).get("statistics", {})

    message_parts = []

    # Surcharge sync stats
    if results.get("surcharge_sync"):
        surcharge_stats = results["surcharge_sync"]
        message_parts.append(f"Surcharge: {surcharge_stats.get('added', 0)} added")

    message_parts.extend([
        f"Categories: {cat_stats.get('synced', 0)}/{cat_stats.get('total', 0)}",
        f"Products: {prod_stats.get('synced', 0)}/{prod_stats.get('total_checked', 0)}"
    ])

    if results.get("variant_cleanup"):
        var_stats = results["variant_cleanup"]
        message_parts.append(f"Variants deleted: {var_stats.get('total_deleted', 0)}")

    if cleanup_orphaned_categories and results.get("category_cleanup"):
        cleanup_stats = results["category_cleanup"].get("statistics", {})
        message_parts.append(f"Categories deleted: {cleanup_stats.get('deleted', 0)}")

    results["message"] = ". ".join(message_parts)
    if dry_run:
        results["message"] += " (DRY RUN)"

    create_shopware_log(
        status="Success",
        message=results["message"],
        method="full_reconciliation"
    )

    return results


@temp_shopware_session
def _cleanup_all_orphaned_variants(client) -> dict[str, Any]:
    """
    Cleanup orphaned variants for all template products.

    Iterates through all ERPNext template items that are synced to Shopware
    and removes variants in Shopware that don't exist in ERPNext.

    Returns:
        Dict with cleanup statistics
    """
    from ecommerce_integrations.shopware6.export.template_handler import cleanup_orphaned_variants

    stats = {
        "templates_checked": 0,
        "total_deleted": 0,
        "errors": []
    }

    # Get all template items with Shopware link
    template_links = frappe.get_all(
        "Ecommerce Item",
        filters={
            "integration": MODULE_NAME,
            "has_variants": 1
        },
        fields=["erpnext_item_code", "integration_item_code"]
    )

    _logger.info(f"[Variant Cleanup] Checking {len(template_links)} templates")

    for link in template_links:
        try:
            cleanup_result = cleanup_orphaned_variants(
                client,
                link.erpnext_item_code,
                link.integration_item_code
            )
            stats["templates_checked"] += 1
            stats["total_deleted"] += cleanup_result.get("deleted", 0)
            stats["errors"].extend(cleanup_result.get("errors", []))

            # Commit periodically with error handling
            if stats["templates_checked"] % 20 == 0:
                try:
                    frappe.db.commit()
                except Exception as commit_error:
                    _logger.warning(f"[Variant Cleanup] Commit failed, trying reconnect: {commit_error}")
                    try:
                        frappe.connect()
                        frappe.db.commit()
                    except Exception:
                        pass

        except Exception as e:
            stats["errors"].append({
                "template": link.erpnext_item_code,
                "error": str(e)[:100]
            })
            # Check for DB connection error and reconnect
            if "InterfaceError" in str(type(e).__name__) or "(0, '')" in str(e):
                try:
                    frappe.connect()
                    _logger.info(f"[Variant Cleanup] Reconnected after DB error for {link.erpnext_item_code}")
                except Exception:
                    pass

    try:
        frappe.db.commit()
    except Exception:
        try:
            frappe.connect()
            frappe.db.commit()
        except Exception:
            pass

    _logger.info(
        f"[Variant Cleanup] Done: {stats['templates_checked']} templates, "
        f"{stats['total_deleted']} variants deleted"
    )

    return stats


@frappe.whitelist()
@temp_shopware_session
def cleanup_orphaned_shopware_categories(
    client,
    root_category: str = None,
    dry_run: bool = True
) -> dict[str, Any]:
    """
    Delete categories in Shopware that no longer exist in ERPNext.

    IMPORTANT: Only affects categories UNDER the root_category in Shopware.
    Categories outside the sync root are never touched.

    Args:
        client: Shopware API client
        root_category: ERPNext root category to compare against
        dry_run: If True, only report what would be deleted

    Returns:
        Cleanup results with statistics
    """
    dry_run = _parse_bool(dry_run)

    setting = frappe.get_cached_doc(SETTING_DOCTYPE)
    if not setting.is_enabled():
        return {"success": False, "message": "Shopware integration is not enabled"}

    if not root_category:
        root_category = getattr(setting, 'category_sync_root', 'Products') or 'Products'

    # Get ERPNext categories under root
    root_info = frappe.db.get_value("Item Group", root_category, ["lft", "rgt"], as_dict=True)
    if not root_info:
        return {"success": False, "message": f"Root category '{root_category}' not found in ERPNext"}

    erpnext_categories = set(frappe.get_all(
        "Item Group",
        filters=[["lft", ">=", root_info.lft], ["rgt", "<=", root_info.rgt]],
        pluck="name"
    )) - ROOT_ITEM_GROUPS

    # Step 1: Find the Shopware root category by name
    try:
        root_response = client.request_post("search/category", {
            "filter": [{"type": "equals", "field": "name", "value": root_category}],
            "limit": 1
        })
        root_cats = root_response.get("data", [])

        if not root_cats:
            return {
                "success": False,
                "message": f"Root category '{root_category}' not found in Shopware. "
                          "Only categories under this root would be cleaned up."
            }

        shopware_root_id = root_cats[0].get("id")
        shopware_root_path = root_cats[0].get("path") or ""

        frappe.logger("shopware6").info(
            f"Found Shopware root category '{root_category}' with ID {shopware_root_id}"
        )

    except Exception as e:
        return {"success": False, "message": f"Failed to find Shopware root category: {e}"}

    # Step 2: Get ONLY Shopware categories that are descendants of the root
    # Shopware uses 'path' field which contains ancestor IDs separated by |
    try:
        all_shopware_cats = []
        page = 1
        while True:
            response = client.request_post("search/category", {
                "limit": 100, "page": page,
                "filter": [
                    # Category must have a path that contains the root ID
                    {"type": "contains", "field": "path", "value": shopware_root_id}
                ]
            })
            cats = response.get("data", [])
            if not cats:
                break
            all_shopware_cats.extend(cats)
            if len(cats) < 100:
                break
            page += 1

        # Also add direct children of root (their path might not contain root_id yet)
        response = client.request_post("search/category", {
            "limit": 500,
            "filter": [{"type": "equals", "field": "parentId", "value": shopware_root_id}]
        })
        direct_children = response.get("data", [])
        existing_ids = {c["id"] for c in all_shopware_cats}
        for child in direct_children:
            if child["id"] not in existing_ids:
                all_shopware_cats.append(child)

    except Exception as e:
        return {"success": False, "message": f"Failed to fetch Shopware categories: {e}"}

    # Step 3: Find orphaned categories (in Shopware under root but not in ERPNext)
    # Protect category trees managed directly in Shopware (not via ERPNext sync).
    # IDs are configured in Shopware Setting > Protected Shopware Category IDs.
    protected_ids_raw = getattr(setting, 'protected_shopware_categories', '') or ''
    protected_category_ids = {
        line.strip() for line in protected_ids_raw.splitlines() if line.strip()
    }

    def _is_protected(cat):
        if not protected_category_ids:
            return False
        cat_id = cat.get("id", "")
        cat_path = cat.get("path") or ""
        if cat_id in protected_category_ids:
            return True
        return any(pid in cat_path for pid in protected_category_ids)

    orphaned = [
        {"id": c.get("id"), "name": c.get("name"), "parent_id": c.get("parentId")}
        for c in all_shopware_cats
        if c.get("name") and c.get("name") not in erpnext_categories
        and c.get("id") != shopware_root_id  # Never delete the root itself
        and not _is_protected(c)  # Never delete protected category trees
    ]

    stats = {
        "root_category": root_category,
        "shopware_root_id": shopware_root_id,
        "erpnext_count": len(erpnext_categories),
        "shopware_count": len(all_shopware_cats),
        "orphaned_count": len(orphaned),
        "deleted": 0, "failed": 0, "errors": []
    }

    if not orphaned:
        return {
            "success": True, "dry_run": dry_run, "statistics": stats,
            "message": f"No orphaned categories found under '{root_category}'"
        }

    if not dry_run:
        # Delete leaf categories first (those without children in orphaned list)
        for cat in orphaned:
            cat["has_children"] = any(o["parent_id"] == cat["id"] for o in orphaned)
        orphaned.sort(key=lambda x: x.get("has_children", False))

        # Batch delete for performance - leaf categories first, then parents
        category_ids = [cat["id"] for cat in orphaned]
        stats["deleted"] = _batch_delete_categories(client, category_ids)
        stats["failed"] = len(orphaned) - stats["deleted"]

    return {
        "success": True,
        "dry_run": dry_run,
        "statistics": stats,
        "orphaned_categories": [c["name"] for c in orphaned[:20]],
        "message": f"Found {len(orphaned)} orphaned under '{root_category}', deleted {stats['deleted']}" + (" (DRY RUN)" if dry_run else "")
    }


# =============================================================================
# ORPHANED PRODUCTS CLEANUP
# =============================================================================

@frappe.whitelist()
@temp_shopware_session
def cleanup_orphaned_shopware_products(
    client,
    dry_run: bool = True,
    batch_size: int = 100
) -> dict[str, Any]:
    """
    Delete products in Shopware that no longer exist in ERPNext.

    This function finds all products in Shopware and checks if they have
    a corresponding Item in ERPNext. If not, the product is deleted.

    IMPORTANT:
    - Only deletes products that were originally synced from ERPNext
      (have a productNumber that matches an ERPNext item_code pattern)
    - Deletes both simple products and variants
    - Parent products are deleted AFTER their variants

    Args:
        client: Shopware API client (injected by decorator)
        dry_run: If True, only report what would be deleted
        batch_size: Number of products to fetch per API call

    Returns:
        Cleanup results with statistics
    """
    dry_run = _parse_bool(dry_run)
    batch_size = cint(batch_size) or 100

    setting = frappe.get_cached_doc(SETTING_DOCTYPE)
    if not setting.is_enabled():
        return {"success": False, "message": "Shopware integration is not enabled"}

    stats = {
        "shopware_total": 0,
        "erpnext_exists": 0,
        "orphaned_count": 0,
        "deleted": 0,
        "failed": 0,
        "errors": []
    }

    _logger.info(
        f"[Orphaned Products Cleanup] Starting (dry_run={dry_run}, batch_size={batch_size})"
    )

    def _ensure_db_connection():
        """Ensure database connection is active."""
        try:
            frappe.db.sql("SELECT 1")
        except Exception:
            try:
                frappe.connect()
                _logger.info("[Orphaned Products Cleanup] Reconnected to database")
            except Exception as e:
                _logger.warning(f"[Orphaned Products Cleanup] Reconnect attempt failed: {e}")

    def _safe_commit():
        """Commit with reconnect on failure."""
        try:
            frappe.db.commit()
        except Exception as commit_error:
            _logger.warning(f"[Orphaned Products Cleanup] Commit failed, trying reconnect: {commit_error}")
            try:
                frappe.connect()
                frappe.db.commit()
            except Exception:
                pass

    # Step 1: Get ALL products from Shopware (paginated)
    all_shopware_products = []
    page = 1

    try:
        while True:
            try:
                response = client.request_post("search/product", {
                    "limit": batch_size,
                    "page": page,
                    "fields": ["id", "productNumber", "name", "parentId", "active"],
                    "filter": [
                        # Only get root-level products and variants, not child products of bundles etc.
                        {"type": "multi", "operator": "OR", "queries": [
                            {"type": "equals", "field": "parentId", "value": None},
                            {"type": "not", "queries": [{"type": "equals", "field": "parentId", "value": None}]}
                        ]}
                    ]
                })
            except Exception as api_error:
                # Log and try to continue with what we have
                _logger.error(f"[Orphaned Products Cleanup] API error on page {page}: {api_error}")
                if all_shopware_products:
                    _logger.info(f"[Orphaned Products Cleanup] Continuing with {len(all_shopware_products)} products fetched so far")
                    break
                raise

            products = response.get("data", [])
            if not products:
                break

            all_shopware_products.extend(products)
            stats["shopware_total"] = len(all_shopware_products)

            if page % 10 == 0:
                _logger.info(
                    f"[Orphaned Products Cleanup] Fetched page {page}, total so far: {len(all_shopware_products)}"
                )

            if len(products) < batch_size:
                break
            page += 1

            # Safety limit to prevent infinite loops
            if page > 1000:
                _logger.warning("[Orphaned Products Cleanup] Reached page limit of 1000")
                break

    except Exception as e:
        return {"success": False, "message": f"Failed to fetch Shopware products: {e}"}

    _logger.info(
        f"[Orphaned Products Cleanup] Found {len(all_shopware_products)} products in Shopware"
    )

    # Ensure DB connection is still alive after Shopware pagination
    _ensure_db_connection()

    # Step 2: Get all ERPNext item codes for comparison (using SQL for memory efficiency)
    try:
        erpnext_item_codes = {
            row[0] for row in frappe.db.sql("SELECT name FROM `tabItem`")
        }
    except Exception as db_error:
        _logger.error(f"[Orphaned Products Cleanup] DB error fetching items: {db_error}")
        _ensure_db_connection()
        erpnext_item_codes = {
            row[0] for row in frappe.db.sql("SELECT name FROM `tabItem`")
        }

    _logger.info(
        f"[Orphaned Products Cleanup] Found {len(erpnext_item_codes)} items in ERPNext"
    )

    # Step 3: Find orphaned products (in Shopware but not in ERPNext)
    orphaned_products = []
    orphaned_variants = []

    for product in all_shopware_products:
        product_number = product.get("productNumber", "")
        shopware_id = product.get("id")
        parent_id = product.get("parentId")
        name = product.get("name", "")

        # Check if this product exists in ERPNext
        if product_number in erpnext_item_codes:
            stats["erpnext_exists"] += 1
            continue

        # Product doesn't exist in ERPNext - it's orphaned
        orphaned_item = {
            "id": shopware_id,
            "productNumber": product_number,
            "name": name,
            "parentId": parent_id,
            "active": product.get("active", False)
        }

        if parent_id:
            # This is a variant
            orphaned_variants.append(orphaned_item)
        else:
            # This is a parent/simple product
            orphaned_products.append(orphaned_item)

    stats["orphaned_count"] = len(orphaned_products) + len(orphaned_variants)

    _logger.info(
        f"[Orphaned Products Cleanup] Found {len(orphaned_products)} orphaned parents, "
        f"{len(orphaned_variants)} orphaned variants"
    )

    if stats["orphaned_count"] == 0:
        return {
            "success": True,
            "dry_run": dry_run,
            "statistics": stats,
            "message": "No orphaned products found in Shopware"
        }

    # Step 4: Delete orphaned products (variants first, then parents) using batch API
    if not dry_run:
        # Delete variants first (batch delete for performance)
        if orphaned_variants:
            variant_ids = [v["id"] for v in orphaned_variants]
            _logger.info(f"[Orphaned Products Cleanup] Batch deleting {len(variant_ids)} variants")
            variants_deleted = _batch_delete_products(client, variant_ids)
            stats["deleted"] += variants_deleted
            stats["failed"] += len(variant_ids) - variants_deleted
            _safe_commit()
            _ensure_db_connection()

        # Then delete parent/simple products (batch delete for performance)
        if orphaned_products:
            product_ids = [p["id"] for p in orphaned_products]
            _logger.info(f"[Orphaned Products Cleanup] Batch deleting {len(product_ids)} parent products")
            products_deleted = _batch_delete_products(client, product_ids)
            stats["deleted"] += products_deleted
            stats["failed"] += len(product_ids) - products_deleted
            _safe_commit()
            _ensure_db_connection()

        # Also delete Ecommerce Item records for deleted products
        deleted_product_numbers = [p["productNumber"] for p in orphaned_products + orphaned_variants
                                   if p["productNumber"] not in [e["product"] for e in stats["errors"]]]
        if deleted_product_numbers:
            try:
                # Delete in batches to avoid query size limits
                for batch_start in range(0, len(deleted_product_numbers), 100):
                    batch = deleted_product_numbers[batch_start:batch_start + 100]
                    frappe.db.delete("Ecommerce Item", {
                        "integration": MODULE_NAME,
                        "erpnext_item_code": ("in", batch)
                    })
                _safe_commit()
            except Exception as e:
                _logger.warning(f"[Orphaned Products Cleanup] Failed to delete Ecommerce Items: {e}")
                _ensure_db_connection()

    # Build result
    orphaned_list = [
        {"productNumber": p["productNumber"], "name": (p["name"] or "")[:50], "type": "variant" if p.get("parentId") else "product"}
        for p in (orphaned_variants + orphaned_products)[:50]
    ]

    create_shopware_log(
        status="Success" if stats["failed"] == 0 else "Warning",
        method="cleanup_orphaned_shopware_products",
        message=f"Orphaned: {stats['orphaned_count']}, Deleted: {stats['deleted']}, Failed: {stats['failed']}"
                + (" (DRY RUN)" if dry_run else "")
    )

    return {
        "success": True,
        "dry_run": dry_run,
        "statistics": stats,
        "orphaned_products": orphaned_list,
        "message": f"Found {stats['orphaned_count']} orphaned products, deleted {stats['deleted']}"
                   + (" (DRY RUN)" if dry_run else "")
    }


@frappe.whitelist()
def enqueue_cleanup_orphaned_products(
    dry_run: bool = False,
    batch_size: int = 100
) -> dict[str, Any]:
    """
    Enqueue orphaned products cleanup as a background job.

    Args:
        dry_run: If True, only report what would be deleted
        batch_size: Products per API call

    Returns:
        Job enqueue status
    """
    dry_run = _parse_bool(dry_run)
    batch_size = cint(batch_size) or 100

    job_name = f"shopware6_cleanup_orphaned_products_{now()}"

    frappe.enqueue(
        "ecommerce_integrations.shopware6.export.reconciliation.cleanup_orphaned_shopware_products",
        queue="long",
        timeout=3600,  # 1 hour
        job_name=job_name,
        dry_run=dry_run,
        batch_size=batch_size
    )

    create_shopware_log(
        status="Queued",
        method="cleanup_orphaned_shopware_products",
        message=f"Cleanup queued (dry_run={dry_run}, batch_size={batch_size})"
    )

    return {
        "success": True,
        "job_name": job_name,
        "message": f"Orphaned products cleanup enqueued" + (" (DRY RUN)" if dry_run else "")
    }


# =============================================================================
# BACKGROUND JOB SUPPORT
# =============================================================================

RECONCILIATION_JOB_NAME = "shopware6.reconciliation.full"


# DEPRECATED
def enqueue_full_reconciliation(
    batch_size: int = 50,
    sync_images: bool = True,
    compare_categories: bool = True
) -> dict[str, Any]:
    """
    DEPRECATED: Use sync_manager.enqueue_full_reconciliation_no_brainer() instead.
    This function uses single-item uploads which are much slower than the batch approach.

    Enqueue full reconciliation to run in background.

    Uses batch processing with commits to avoid transaction errors.

    Args:
        batch_size: Items per batch
        sync_images: Sync images for changed items
        compare_categories: Compare categories

    Returns:
        Job enqueue status
    """
    return {
        "success": False,
        "message": "Deprecated. Use sync_manager.enqueue_full_reconciliation_no_brainer() instead.",
    }
    from frappe.utils.background_jobs import is_job_enqueued

    sync_images = _parse_bool(sync_images)
    compare_categories = _parse_bool(compare_categories)

    if is_job_enqueued(RECONCILIATION_JOB_NAME):
        return {
            "success": False,
            "message": "A reconciliation job is already running."
        }

    log = create_shopware_log(
        status="Queued",
        method="enqueue_full_reconciliation",
        message="Full reconciliation queued"
    )

    frappe.enqueue(
        "ecommerce_integrations.shopware6.export.reconciliation._run_batch_reconciliation",
        queue="long",
        timeout=14400,  # 4 hours
        job_name=RECONCILIATION_JOB_NAME,
        batch_size=cint(batch_size),
        sync_images=sync_images,
        compare_categories=compare_categories,
        log_name=log.name if log else None
    )

    return {
        "success": True,
        "message": f"Reconciliation job enqueued (batch_size={batch_size})",
        "job_queue": "long",
        "log_name": log.name if log else None
    }


# DEPRECATED
def enqueue_full_reconciliation_with_categories(
    limit: int = 500,
    dry_run: bool = False,
    sync_images: bool = False,
    include_unlinked: bool = False,
    category_root: str = None,
    skip_root_category: bool = None,
    sync_empty_categories: bool = None,
    cleanup_orphaned_categories: bool = False
) -> dict[str, Any]:
    """
    DEPRECATED: Use sync_manager.enqueue_full_reconciliation_no_brainer() instead.

    Enqueue full reconciliation (categories + products) as background job.

    Args:
        limit: Maximum products to process
        dry_run: Only report differences
        sync_images: Sync images for changed items
        include_unlinked: Sync products not yet in Shopware
        category_root: Root category (uses setting default if not provided)
        skip_root_category: Skip root category
        sync_empty_categories: Sync empty categories
        cleanup_orphaned_categories: Delete orphaned categories

    Returns:
        Job enqueue status
    """
    return {
        "success": False,
        "message": "Deprecated. Use sync_manager.enqueue_full_reconciliation_no_brainer() instead.",
    }
    # Parse parameters
    dry_run = _parse_bool(dry_run)
    sync_images = _parse_bool(sync_images)
    include_unlinked = _parse_bool(include_unlinked)
    cleanup_orphaned_categories = _parse_bool(cleanup_orphaned_categories)

    setting = frappe.get_cached_doc(SETTING_DOCTYPE)

    if category_root is None:
        category_root = getattr(setting, 'category_sync_root', 'Products') or 'Products'
    if skip_root_category is None:
        skip_root_category = getattr(setting, 'skip_root_category', False)
    if sync_empty_categories is None:
        sync_empty_categories = getattr(setting, 'sync_empty_categories', True)

    skip_root_category = _parse_bool(skip_root_category)
    sync_empty_categories = _parse_bool(sync_empty_categories)

    frappe.enqueue(
        "ecommerce_integrations.shopware6.export.reconciliation.full_reconciliation",
        queue="long",
        timeout=3600,
        job_name=f"shopware6_full_reconciliation_{frappe.utils.now()}",
        limit=cint(limit),
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
        "message": f"Full reconciliation enqueued (categories: '{category_root}', products: {limit}{cleanup_info})",
        "dry_run": dry_run
    }


def _run_batch_reconciliation(
    batch_size: int = 50,
    sync_images: bool = True,
    compare_categories: bool = True,
    log_name: str = None
):
    """
    Internal batch reconciliation runner.

    Processes all items in batches with:
    - Commits between batches
    - Fresh Shopware session per batch
    - Progress logging
    """
    from ecommerce_integrations.shopware6.export.product_uploader import upload_erpnext_item_to_shopware

    setting = frappe.get_cached_doc(SETTING_DOCTYPE)
    if not setting.is_enabled():
        _logger.error("Shopware integration not enabled")
        return

    # Get ALL linked items
    ecom_items = frappe.get_all(
        "Ecommerce Item",
        filters={"integration": MODULE_NAME},
        fields=["erpnext_item_code", "integration_item_code"],
        order_by="modified desc"
    )

    total_items = len(ecom_items)
    _logger.info(f"[Reconciliation] Starting for {total_items} items")

    stats = {
        "total": total_items, "processed": 0, "in_sync": 0,
        "synced": 0, "errors": 0, "start_time": now()
    }

    batch_num = 0
    for batch in create_batch(ecom_items, batch_size):
        batch_num += 1

        try:
            client = get_shopware_client()
            shopware_ids = [item.integration_item_code for item in batch]
            shopware_products = get_shopware_products_batch(client, shopware_ids, compare_categories)

            for ecom_item in batch:
                item_code = ecom_item.erpnext_item_code
                shopware_id = ecom_item.integration_item_code

                try:
                    erpnext_item = frappe.get_doc("Item", item_code)
                    shopware_data = shopware_products.get(shopware_id)

                    if not shopware_data:
                        stats["errors"] += 1
                        stats["processed"] += 1
                        continue

                    comparison = compare_item_with_shopware(
                        erpnext_item, shopware_data, compare_categories
                    )

                    if comparison["needs_sync"]:
                        orig_img = getattr(setting, 'sync_images_to_shopware', True)
                        if not sync_images:
                            setting.sync_images_to_shopware = False

                        upload_erpnext_item_to_shopware(item_code)
                        setting.sync_images_to_shopware = orig_img
                        stats["synced"] += 1
                    else:
                        stats["in_sync"] += 1

                    stats["processed"] += 1

                except Exception as e:
                    stats["errors"] += 1
                    stats["processed"] += 1
                    _logger.warning(f"[Reconciliation] Error: {item_code}: {e}")

            frappe.db.commit()

            # Log progress every 5 batches
            if batch_num % 5 == 0:
                pct = round(stats["processed"] / total_items * 100, 1)
                _logger.info(
                    f"[Reconciliation] {pct}% - Synced: {stats['synced']}, "
                    f"In Sync: {stats['in_sync']}, Errors: {stats['errors']}"
                )

        except Exception as e:
            _logger.error(f"[Reconciliation] Batch {batch_num} failed: {e}")
            try:
                frappe.db.rollback()
            except Exception:
                pass

            # Reconnect to database after connection error
            try:
                frappe.db.sql("SELECT 1")
            except Exception:
                try:
                    frappe.connect()
                    _logger.info(f"[Reconciliation] Reconnected to database after batch {batch_num} failure")
                except Exception as reconnect_error:
                    _logger.error(f"[Reconciliation] Failed to reconnect: {reconnect_error}")

    stats["end_time"] = now()
    _logger.info(
        f"[Reconciliation] COMPLETED - Synced: {stats['synced']}, "
        f"In Sync: {stats['in_sync']}, Errors: {stats['errors']}"
    )

    if log_name:
        try:
            frappe.db.set_value("Shopware Log", log_name, {
                "status": "Success",
                "message": f"Completed: {stats['synced']} synced, {stats['in_sync']} in sync, {stats['errors']} errors"
            })
            frappe.db.commit()
        except Exception:
            pass

    return stats


# =============================================================================
# FORCE IMAGE SYNC (Parallel version - optimized)
# =============================================================================

@frappe.whitelist()
def enqueue_force_sync_all_images_parallel(
    batch_size: int = 100,
    workers: int = 4,
    item_group: str = None,
    parent_item: str = None,
    start_batch: int = 1,
) -> dict[str, Any]:
    """
    Enqueue PARALLEL force image sync as a background job.

    Much faster than sequential sync - uses multiple worker threads.

    Args:
        batch_size: Number of items per batch (default 100)
        workers: Number of parallel workers (default 4, max 8)
        item_group: Optional - only sync items in this item group
        parent_item: Optional - only sync variants of this parent
        start_batch: Start from this batch number (1-based, for resuming interrupted syncs)

    Returns:
        Dict with job info
    """
    batch_size = cint(batch_size) or 100
    workers = min(cint(workers) or 4, 8)  # Cap at 8 workers
    start_batch = cint(start_batch) or 1

    # Build parameterized query to prevent SQL injection
    base_conditions = """
        ei.integration = 'Shopware6'
        AND i.image IS NOT NULL AND i.image != ''
    """
    params = []

    if item_group:
        base_conditions += " AND i.item_group = %s"
        params.append(item_group)
    if parent_item:
        base_conditions += " AND i.variant_of = %s"
        params.append(parent_item)

    # Count total items using parameterized query
    total = frappe.db.sql(f"""
        SELECT COUNT(*) FROM `tabEcommerce Item` ei
        INNER JOIN `tabItem` i ON i.name = ei.erpnext_item_code
        WHERE {base_conditions}
    """, tuple(params))[0][0]

    if total == 0:
        return {
            "success": False,
            "message": "No items found matching criteria",
        }

    job_name = f"shopware6_parallel_image_sync_{now()}"

    frappe.enqueue(
        "ecommerce_integrations.shopware6.export.reconciliation._run_parallel_image_sync",
        queue="long",
        job_name=job_name,
        timeout=3600 * 6,  # 6 hours
        batch_size=batch_size,
        workers=workers,
        total=total,
        base_conditions=base_conditions,
        query_params=params,
        start_batch=start_batch,
    )

    start_info = f" starting from batch {start_batch}" if start_batch > 1 else ""
    return {
        "success": True,
        "message": f"Parallel image sync enqueued for {total} products ({workers} workers, batch size: {batch_size}){start_info}",
        "job_name": job_name,
    }


def _run_parallel_image_sync(
    batch_size: int,
    workers: int,
    total: int,
    base_conditions: str,
    query_params: list = None,
    start_batch: int = 1,
):
    """
    Run force image sync with parallel processing.

    OPTIMIZED: Each worker thread initializes Frappe context and Shopware client
    ONCE and reuses them for all items in their work queue. This avoids the
    massive overhead of per-item initialization.

    Args:
        batch_size: Number of items per batch
        workers: Number of parallel workers
        total: Total items to process
        base_conditions: SQL WHERE conditions (parameterized)
        query_params: Parameters for the SQL query
        start_batch: Start from this batch number (1-based, for resuming interrupted syncs)
    """
    import gc
    import queue
    import threading
    import traceback
    from ecommerce_integrations.shopware6.export.image_handler import sync_product_images_to_shopware
    from ecommerce_integrations.shopware6.base.cache_manager import get_cache, reset_thread_cache

    query_params = query_params or []

    # Test connection upfront
    client = get_shopware_client()
    if not client:
        create_shopware_log(
            status="Error",
            method="parallel_image_sync",
            message="No Shopware client available",
        )
        return

    # Get site name for thread initialization
    site_name = frappe.local.site

    # Thread-safe stats with lock
    stats = {"processed": 0, "synced": 0, "failed": 0, "errors": []}
    stats_lock = threading.Lock()
    num_batches = (total + batch_size - 1) // batch_size

    # Validate start_batch
    start_batch = max(1, start_batch)
    start_info = f" (starting from batch {start_batch})" if start_batch > 1 else ""

    # Work queue for items - threads pull from this
    work_queue = queue.Queue()
    # Sentinel to signal workers to stop
    STOP_SENTINEL = None

    log_doc = create_shopware_log(
        status="Queued",
        method="parallel_image_sync",
        message=f"Starting parallel image sync for {total} products ({workers} workers, {num_batches} batches){start_info}",
    )
    log_name = log_doc.name

    def worker_thread(worker_id: int) -> None:
        """
        Worker thread that processes items from the queue.

        OPTIMIZATION: Initializes Frappe context and Shopware client ONCE,
        then processes all items from the queue using the same context.
        """
        thread_client = None
        thread_cache = None
        items_processed = 0
        # FIX: Timeout escape to prevent infinite loop if SENTINEL is missed
        empty_cycles = 0
        MAX_EMPTY_CYCLES = 10  # Exit after 10 seconds of no work

        try:
            # Initialize Frappe context ONCE for this thread
            frappe.init(site=site_name)
            frappe.connect()

            # Create Shopware client ONCE for this thread
            thread_client = get_shopware_client()
            if not thread_client:
                _logger.error(f"[Worker {worker_id}] Could not get Shopware client")
                return

            # Get thread-local cache ONCE
            thread_cache = get_cache()

            # Track token age for periodic refresh (Shopware tokens expire after ~10min)
            import time as _time
            _token_created_at = _time.time()
            _TOKEN_REFRESH_INTERVAL = 480  # Refresh every 8 minutes (token expires at 10)

            _logger.info(f"[Worker {worker_id}] Initialized - ready to process items")

            # Process items from queue until we get the stop sentinel
            while True:
                try:
                    item_data = work_queue.get(timeout=1)
                    empty_cycles = 0  # Reset on successful get
                except queue.Empty:
                    empty_cycles += 1
                    # FIX: Escape hatch to prevent infinite loop
                    if empty_cycles >= MAX_EMPTY_CYCLES:
                        _logger.warning(f"[Worker {worker_id}] Timeout after {MAX_EMPTY_CYCLES}s - exiting")
                        break
                    continue

                if item_data is STOP_SENTINEL:
                    work_queue.task_done()
                    break

                item_code = item_data["erpnext_item_code"]
                shopware_id = item_data["integration_item_code"]
                success = False
                error_msg = None

                try:
                    # Refresh Shopware token before it expires
                    if _time.time() - _token_created_at > _TOKEN_REFRESH_INTERVAL:
                        try:
                            thread_client.token = None
                            thread_client._get_session()
                            _token_created_at = _time.time()
                            _logger.info(f"[Worker {worker_id}] Token refreshed")
                        except Exception as token_err:
                            _logger.error(f"[Worker {worker_id}] Token refresh failed: {token_err}")

                    item = frappe.get_doc("Item", item_code)
                    # FIX: Removed premature cache.clear_image_hashes() -
                    # sync_product_images_to_shopware handles cache internally
                    # This was breaking delta-sync by clearing hashes before check

                    result = sync_product_images_to_shopware(
                        thread_client, item, shopware_id, cache=thread_cache
                    )

                    if result is True:
                        success = True
                    else:
                        # result is an error string
                        success = False
                        error_msg = str(result)[:200] if result else "Unknown error"

                    del item
                    items_processed += 1

                    # Periodic commit to avoid long transactions
                    if items_processed % 50 == 0:
                        try:
                            frappe.db.commit()
                        except Exception:
                            pass

                except (KeyboardInterrupt, SystemExit):
                    work_queue.task_done()
                    raise
                except Exception as e:
                    error_msg = str(e)[:200]
                    _logger.error(f"[Worker {worker_id}] Error syncing {item_code}: {error_msg}")

                # Update stats
                with stats_lock:
                    stats["processed"] += 1
                    if success:
                        stats["synced"] += 1
                    else:
                        stats["failed"] += 1
                        if error_msg and len(stats["errors"]) < 50:
                            stats["errors"].append({"item_code": item_code, "error": error_msg})

                work_queue.task_done()

            _logger.info(f"[Worker {worker_id}] Finished - processed {items_processed} items")

        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as e:
            _logger.error(f"[Worker {worker_id}] Fatal error: {e}\n{traceback.format_exc()}")
        finally:
            # Clean up thread context ONCE at the end
            try:
                reset_thread_cache()
            except Exception:
                pass
            try:
                frappe.db.commit()
            except Exception:
                pass
            try:
                frappe.destroy()
            except Exception:
                pass

    # Start worker threads
    # FIX: Non-daemon threads to ensure proper cleanup (finally blocks run)
    worker_threads = []
    for i in range(workers):
        t = threading.Thread(target=worker_thread, args=(i,), name=f"ImageSyncWorker-{i}")
        t.daemon = False  # Ensure finally blocks run for proper DB/cache cleanup
        t.start()
        worker_threads.append(t)

    # Feed items to the queue in batches
    for batch_num in range(num_batches):
        # Skip batches before start_batch (batch_num is 0-based, start_batch is 1-based)
        if batch_num + 1 < start_batch:
            continue

        offset = batch_num * batch_size

        _logger.info(f"[parallel_image_sync] Loading batch {batch_num + 1}/{num_batches} (offset={offset})")

        try:
            sql_params = tuple(query_params) + (batch_size, offset)
            items = frappe.db.sql(f"""
                SELECT ei.erpnext_item_code, ei.integration_item_code
                FROM `tabEcommerce Item` ei
                INNER JOIN `tabItem` i ON i.name = ei.erpnext_item_code
                WHERE {base_conditions}
                ORDER BY ei.erpnext_item_code
                LIMIT %s OFFSET %s
            """, sql_params, as_dict=True)

            _logger.info(f"[parallel_image_sync] Batch {batch_num + 1}: Queuing {len(items)} items")
        except Exception as e:
            _logger.error(f"[parallel_image_sync] SQL error in batch {batch_num + 1}: {e}")
            update_shopware_log(
                log_name,
                status="Error",
                message=f"SQL error in batch {batch_num + 1}: {str(e)[:200]}",
            )
            break

        if not items:
            _logger.info(f"[parallel_image_sync] No items in batch {batch_num + 1}, stopping")
            break

        # Add items to work queue
        for item in items:
            work_queue.put(item)

        # Wait for batch to complete before loading next
        work_queue.join()

        # Memory cleanup after each batch
        gc.collect()

        # Update progress on single log entry
        with stats_lock:
            pct = round(stats["processed"] / total * 100, 1) if total > 0 else 0
            synced = stats["synced"]
            failed = stats["failed"]
            current_errors = list(stats["errors"])
        update_shopware_log(
            log_name,
            status="Success",
            message=f"Batch {batch_num + 1}/{num_batches} ({pct}%) - Synced: {synced}, Failed: {failed}",
            response_data={
                "progress": f"{pct}%",
                "synced": synced,
                "failed": failed,
                "errors": current_errors,
            } if current_errors else {"progress": f"{pct}%", "synced": synced, "failed": failed},
        )

    # Signal workers to stop
    for _ in range(workers):
        work_queue.put(STOP_SENTINEL)

    # Wait for all workers to finish with extended timeout
    # FIX: Longer timeout and logging for non-terminated threads
    for t in worker_threads:
        t.join(timeout=120)  # 2 minutes per worker
        if t.is_alive():
            _logger.error(f"[parallel_image_sync] Worker {t.name} did not terminate within timeout")

    with stats_lock:
        final_synced = stats["synced"]
        final_failed = stats["failed"]
        final_errors = list(stats["errors"])
    final_status = "Success" if final_failed == 0 else "Error"
    update_shopware_log(
        log_name,
        status=final_status,
        message=f"COMPLETED - Synced: {final_synced}, Failed: {final_failed}",
        response_data={
            "synced": final_synced,
            "failed": final_failed,
            "errors": final_errors,
        },
    )


# =============================================================================
# FORCE VARIANT SYNC (BATCH API)
# =============================================================================

# Default chunk settings for large templates (can be overridden in Shopware Setting)
DEFAULT_VARIANT_CHUNK_SIZE = 25
DEFAULT_VARIANT_CHUNK_DELAY = 2.0  # seconds between chunks to let Shopware DB recover


def _get_variant_chunk_settings() -> tuple[int, float]:
    """
    Get variant chunk settings from Shopware Setting or use defaults.

    Returns:
        Tuple of (chunk_size, chunk_delay_seconds)
    """
    try:
        setting = frappe.get_cached_doc(SETTING_DOCTYPE)
        chunk_size = cint(getattr(setting, 'variant_chunk_size', 0)) or DEFAULT_VARIANT_CHUNK_SIZE
        chunk_delay = flt(getattr(setting, 'variant_chunk_delay', 0)) or DEFAULT_VARIANT_CHUNK_DELAY
        return chunk_size, chunk_delay
    except Exception:
        return DEFAULT_VARIANT_CHUNK_SIZE, DEFAULT_VARIANT_CHUNK_DELAY


def _batch_delete_products(client, product_ids: list[str]) -> int:
    """
    Delete multiple products via Shopware Sync API (batch operation).

    Args:
        client: Shopware API client
        product_ids: List of Shopware product IDs to delete

    Returns:
        Number of successfully deleted products
    """
    if not product_ids:
        return 0

    import time as _time
    logger = get_logger("batch_delete")
    deleted = 0

    # Process in small batches to avoid Shopware DB lock timeouts
    # Reduced from 100 to 10 after issues with large templates having many variants
    batch_size = 10
    for i in range(0, len(product_ids), batch_size):
        batch_ids = product_ids[i:i + batch_size]

        # Shopware Sync API delete format
        sync_payload = {
            "delete-products": {
                "entity": "product",
                "action": "delete",
                "payload": [{"id": pid} for pid in batch_ids]
            }
        }

        # Disable indexing during bulk deletes to prevent lock conflicts with background indexer
        # See: https://github.com/shopware/admin-api-reference/blob/main/docs/concepts/endpoint-structure/writing-entities/bulk-payloads.md
        # indexing-skip: Skip the product indexer entirely (includes VariantListingUpdater which causes display_group updates)
        headers = {
            "indexing-behavior": "disable-indexing",
            "indexing-skip": "product.indexer",
            "sw-skip-trigger-flow": "1"
        }

        # Retry logic for lock timeouts (up to 5 attempts with exponential backoff)
        max_retries = 5
        for attempt in range(1, max_retries + 1):
            try:
                client.request_post("_action/sync", sync_payload, update_header_fields=headers)
                deleted += len(batch_ids)
                break  # Success, exit retry loop

            except BaseException as e:
                # Note: ShopwareAPIError inherits from BaseException, not Exception!
                error_str = str(e)
                is_lock_timeout = "1205" in error_str or "Lock wait timeout" in error_str

                if is_lock_timeout and attempt < max_retries:
                    # Exponential backoff: 5s, 10s, 20s, 40s
                    wait_time = 5 * (2 ** (attempt - 1))
                    logger.warning(f"Lock timeout (attempt {attempt}/{max_retries}), retrying in {wait_time}s...")
                    _time.sleep(wait_time)
                    continue

                if is_lock_timeout:
                    # Max retries exceeded - try individual deletes as last resort
                    logger.warning(f"Lock timeout after {max_retries} retries, falling back to individual deletes")
                    for pid in batch_ids:
                        try:
                            _time.sleep(2)  # 2 seconds between each delete for DB recovery
                            client.request_delete(f"product/{pid}")
                            deleted += 1
                        except BaseException:
                            # ShopwareAPIError inherits from BaseException, not Exception
                            pass
                    break

                # Non-lock-timeout error: fallback to individual deletes
                logger.warning(f"Batch delete failed for {len(batch_ids)} products: {e}")
                for pid in batch_ids:
                    try:
                        _time.sleep(1)  # 1 second between each delete
                        client.request_delete(f"product/{pid}")
                        deleted += 1
                    except BaseException:
                        # ShopwareAPIError inherits from BaseException, not Exception
                        pass
                break  # Exit retry loop after fallback

        # Small delay between batches to let Shopware DB recover
        if i + batch_size < len(product_ids):
            _time.sleep(1)

    return deleted


def _batch_delete_categories(client, category_ids: list[str]) -> int:
    """
    Delete multiple categories via Shopware Sync API (batch operation).

    Note: Categories with children may fail in batch delete. In that case,
    the function falls back to individual deletes.

    Args:
        client: Shopware API client
        category_ids: List of Shopware category IDs to delete

    Returns:
        Number of successfully deleted categories
    """
    if not category_ids:
        return 0

    logger = get_logger("batch_delete")
    deleted = 0

    # Process in batches of 100
    batch_size = 100
    for i in range(0, len(category_ids), batch_size):
        batch_ids = category_ids[i:i + batch_size]

        # Shopware Sync API delete format
        sync_payload = {
            "delete-categories": {
                "entity": "category",
                "action": "delete",
                "payload": [{"id": cid} for cid in batch_ids]
            }
        }

        # Retry logic for lock timeouts (up to 3 attempts with 5s delay)
        max_retries = 3
        for attempt in range(1, max_retries + 1):
            try:
                client.request_post("_action/sync", sync_payload)
                deleted += len(batch_ids)
                break  # Success, exit retry loop

            except Exception as e:
                error_str = str(e)
                is_lock_timeout = "1205" in error_str or "Lock wait timeout" in error_str

                if is_lock_timeout and attempt < max_retries:
                    logger.warning(f"Lock timeout on category delete (attempt {attempt}/{max_retries}), retrying in 5s...")
                    import time
                    time.sleep(5)
                    continue

                if is_lock_timeout:
                    # Max retries exceeded for lock timeout - log error and raise to stop sync
                    error_msg = f"Lock timeout after {max_retries} retries for batch delete of {len(batch_ids)} categories"
                    logger.error(error_msg)
                    create_shopware_log(
                        status="Error",
                        method="batch_delete_categories",
                        message=error_msg,
                        request_data={"category_ids": batch_ids[:10], "error": error_str[:500]}
                    )
                    raise Exception(error_msg)

                # Non-lock-timeout error: fallback to individual deletes
                logger.warning(f"Batch delete failed for {len(batch_ids)} categories: {e}")
                for cid in batch_ids:
                    try:
                        client.request_delete(f"category/{cid}")
                        deleted += 1
                    except Exception:
                        pass
                break  # Exit retry loop after fallback

    return deleted


@frappe.whitelist()
@temp_shopware_session
def force_sync_all_variants(
    client,
    limit: int = 0,
    dry_run: bool = False
) -> dict[str, Any]:
    """
    Force re-sync all variant products to Shopware using Batch API.

    This function:
    1. Finds all template products (has_variants=1) synced to Shopware
    2. For each template: deletes ALL variants in Shopware (batch)
    3. Re-uploads all variants from ERPNext using BatchProductUploader

    Args:
        client: Shopware API client (injected by decorator)
        limit: Max products to process (0 = all)
        dry_run: Preview changes without applying

    Returns:
        Dict with sync statistics
    """
    from ecommerce_integrations.shopware6.export.batch_uploader import BatchProductUploader
    from ecommerce_integrations.shopware6.export.template_handler import (
        upload_template_item_to_shopware,
        clear_product_configurator_settings,
    )

    logger = get_logger("force_sync_all_variants")

    stats = {
        "templates_processed": 0,
        "variants_deleted": 0,
        "variants_synced": 0,
        "failed": 0,
        "dry_run": dry_run,
        "errors": []
    }

    create_shopware_log(
        status="Info",
        method="force_sync_all_variants",
        message=f"Starting force variant sync with Batch API (limit={limit}, dry_run={dry_run})"
    )

    # Get all template items synced to Shopware
    filters = {
        "integration": MODULE_NAME,
        "has_variants": 1
    }

    template_ecom_items = frappe.get_all(
        "Ecommerce Item",
        filters=filters,
        fields=["erpnext_item_code", "integration_item_code"],
        limit=limit if limit > 0 else None
    )

    total = len(template_ecom_items)
    logger.info(f"Found {total} template products to process")

    # Initialize BatchProductUploader once
    uploader = BatchProductUploader()

    for idx, ecom_item in enumerate(template_ecom_items):
        template_code = ecom_item.erpnext_item_code
        parent_shopware_id = ecom_item.integration_item_code

        try:
            # Get ERPNext variants (including disabled - they sync as active=false)
            erpnext_variants = frappe.get_all(
                "Item",
                filters={"variant_of": template_code},
                pluck="name"
            )

            if not erpnext_variants:
                logger.info(f"{template_code} has no variants, skipping")
                continue

            # Step 1: Get all Shopware variants for this parent
            try:
                response = client.request_post("search/product", {
                    "filter": [{"type": "equals", "field": "parentId", "value": parent_shopware_id}],
                    "limit": 500
                })
                shopware_variants = response.get("data", [])
            except Exception as e:
                logger.warning(f"Could not fetch Shopware variants for {template_code}: {e}")
                shopware_variants = []

            # Step 2: Delete ALL variants in Shopware using batch API
            if not dry_run:
                variant_ids = [v.get("id") for v in shopware_variants if v.get("id")]
                deleted_count = _batch_delete_products(client, variant_ids)
                stats["variants_deleted"] += deleted_count

                # Clear configurator settings from parent
                clear_product_configurator_settings(client, parent_shopware_id)

                # Delete Ecommerce Item records for variants
                frappe.db.delete("Ecommerce Item", {
                    "integration": MODULE_NAME,
                    "variant_of": template_code
                })
            else:
                stats["variants_deleted"] += len(shopware_variants)

            # Step 3: Re-sync template (to rebuild configuratorSettings)
            if not dry_run:
                template_item = frappe.get_doc("Item", template_code)
                upload_template_item_to_shopware(client, template_item)

            # Step 4: Re-upload all variants using BatchProductUploader
            if not dry_run:
                result = uploader.upload_variants(erpnext_variants, skip_images=True)
                stats["variants_synced"] += result.success
                stats["failed"] += result.failed
                for err in result.errors:
                    stats["errors"].append(f"{err.get('item_code', '?')}: {err.get('error', '')[:100]}")
            else:
                stats["variants_synced"] += len(erpnext_variants)

            stats["templates_processed"] += 1

            if (idx + 1) % 5 == 0:
                logger.info(
                    f"Progress: {idx + 1}/{total} templates, "
                    f"deleted: {stats['variants_deleted']}, synced: {stats['variants_synced']}",
                    persist=True
                )
                if not dry_run:
                    frappe.db.commit()

        except Exception as e:
            stats["failed"] += 1
            stats["errors"].append(f"{template_code}: {str(e)[:100]}")
            logger.warning(f"Error for {template_code}: {e}")

    create_shopware_log(
        status="Success",
        method="force_sync_all_variants",
        message=f"Force variant sync completed (Batch API): {stats['templates_processed']} templates, "
                f"{stats['variants_deleted']} deleted, {stats['variants_synced']} synced, "
                f"{stats['failed']} failed"
    )

    return {
        "success": True,
        "stats": stats,
        "message": f"Processed {stats['templates_processed']} templates: "
                   f"{stats['variants_deleted']} deleted, {stats['variants_synced']} synced"
    }


@frappe.whitelist()
@temp_shopware_session
def force_sync_single_template_variants(
    client,
    template_item_code: str,
    dry_run: bool = False,
    sync_prices: bool = True,
    price_list: str = "Standard-Vertrieb",
    sync_images: bool = True
) -> dict[str, Any]:
    """
    Force re-sync all variants for a SINGLE template product using Batch API.

    Deletes all variants in Shopware (batch) and re-uploads from ERPNext.
    Prices are synced during variant upload (Multi-Channel support).
    Images are synced by default (can be disabled with sync_images=False).

    Args:
        client: Shopware API client (injected)
        template_item_code: ERPNext template item code (e.g. '4702A-Parent')
        dry_run: Preview mode
        sync_prices: Ignored - prices are always synced during upload
        price_list: Ignored - uses Multi-Channel price lists
        sync_images: Sync images after variant upload (default: True)

    Returns:
        Dict with sync statistics
    """
    from ecommerce_integrations.shopware6.export.batch_uploader import BatchProductUploader
    from ecommerce_integrations.shopware6.export.template_handler import (
        upload_template_item_to_shopware,
        clear_product_configurator_settings,
    )
    from ecommerce_integrations.shopware6.export.utils import get_shopware_document_id

    logger = get_logger("force_sync_single_template_variants")

    stats = {
        "template": template_item_code,
        "variants_deleted": 0,
        "variants_synced": 0,
        "prices_synced": 0,
        "images_synced": 0,
        "failed": 0,
        "dry_run": dry_run,
        "errors": []
    }

    # Get Shopware ID for template
    parent_shopware_id = get_shopware_document_id("Item", template_item_code)
    if not parent_shopware_id:
        return {
            "success": False,
            "message": f"Template {template_item_code} not synced to Shopware"
        }

    logger.info(f"Starting for {template_item_code} (Shopware ID: {parent_shopware_id})")

    try:
        # Get ERPNext variants (including disabled - they sync as active=false)
        erpnext_variants = frappe.get_all(
            "Item",
            filters={"variant_of": template_item_code},
            pluck="name"
        )
        logger.info(f"Found {len(erpnext_variants)} ERPNext variants")

        # Get Shopware variants
        try:
            response = client.request_post("search/product", {
                "filter": [{"type": "equals", "field": "parentId", "value": parent_shopware_id}],
                "limit": 500
            })
            shopware_variants = response.get("data", [])
            logger.info(f"Found {len(shopware_variants)} Shopware variants")
        except Exception as e:
            logger.warning(f"Could not fetch Shopware variants: {e}")
            shopware_variants = []

        # Step 1: Delete ALL variants in Shopware using batch API
        if not dry_run:
            variant_ids = [v.get("id") for v in shopware_variants if v.get("id")]
            stats["variants_deleted"] = _batch_delete_products(client, variant_ids)
            logger.info(f"Deleted {stats['variants_deleted']} variants via batch API")

            # Clear configurator settings from parent
            clear_product_configurator_settings(client, parent_shopware_id)

            # Delete Ecommerce Item records for variants
            frappe.db.delete("Ecommerce Item", {
                "integration": MODULE_NAME,
                "variant_of": template_item_code
            })
            frappe.db.commit()
        else:
            stats["variants_deleted"] = len(shopware_variants)

        # Step 2: Re-sync template (to rebuild configuratorSettings)
        if not dry_run:
            logger.info("Re-syncing template product...")
            template_item = frappe.get_doc("Item", template_item_code)
            upload_template_item_to_shopware(client, template_item)

        # Step 3: Re-upload all variants using BatchProductUploader
        if not dry_run and erpnext_variants:
            logger.info(f"Re-uploading {len(erpnext_variants)} variants via Batch API...")
            uploader = BatchProductUploader()
            # Skip images during batch upload - sync them separately if requested
            result = uploader.upload_variants(erpnext_variants, skip_images=True)

            stats["variants_synced"] = result.success
            stats["failed"] = result.failed
            for err in result.errors:
                stats["errors"].append(f"{err.get('item_code', '?')}: {err.get('error', '')[:100]}")
            
            # Step 4: Sync images if requested
            if sync_images and result.success > 0:
                logger.info(f"Syncing images for {result.success} variants...")
                from ecommerce_integrations.shopware6.export.image_handler import sync_product_images_to_shopware
                from ecommerce_integrations.shopware6.export.utils import get_shopware_document_id as get_sw_id
                
                images_synced = 0
                for variant_code in erpnext_variants:
                    try:
                        variant_item = frappe.get_doc("Item", variant_code)
                        variant_shopware_id = get_sw_id("Item", variant_code)
                        if variant_shopware_id:
                            sync_product_images_to_shopware(client, variant_item, variant_shopware_id)
                            images_synced += 1
                    except Exception as img_err:
                        logger.warning(f"Image sync failed for {variant_code}: {img_err}")
                
                stats["images_synced"] = images_synced
                logger.info(f"Images synced for {images_synced} variants")
        else:
            stats["variants_synced"] = len(erpnext_variants)

        if not dry_run:
            frappe.db.commit()

        # Prices are set during variant upload (with Multi-Channel support)
        stats["prices_synced"] = stats["variants_synced"]

    except Exception as e:
        stats["errors"].append(str(e)[:200])
        logger.error(f"Force single variant sync failed for {template_item_code}", exception=e, persist=True)

    create_shopware_log(
        status="Success" if stats["failed"] == 0 else "Warning",
        method="force_sync_single_template_variants",
        message=f"{template_item_code}: deleted {stats['variants_deleted']}, synced {stats['variants_synced']}, images {stats['images_synced']}, failed {stats['failed']}"
    )

    return {
        "success": True,
        "stats": stats,
        "message": f"Template {template_item_code}: {stats['variants_deleted']} deleted, {stats['variants_synced']} synced, {stats['images_synced']} images"
    }


@frappe.whitelist()
def enqueue_force_sync_all_variants(
    batch_size: int = 10,
    dry_run: bool = False,
    sync_prices: bool = True,
    price_list: str = "Standard-Vertrieb",
    brand: str = None,
    start_batch: int = 1
) -> dict[str, Any]:
    """
    Enqueue force variant sync as a background job.

    Args:
        batch_size: Templates per batch
        dry_run: Preview mode
        sync_prices: Also force sync prices (default: True)
        price_list: Price list for price sync
        brand: Filter by brand (e.g. 'MyBrand')
        start_batch: Start from this batch number (1-based, for resuming interrupted syncs)

    Returns:
        Dict with job info
    """
    # Use batch size from settings if not provided
    if not batch_size:
        setting = frappe.get_cached_doc(SETTING_DOCTYPE)
        batch_size = cint(getattr(setting, 'variant_batch_size', 10)) or 10
    else:
        batch_size = cint(batch_size)
    dry_run = _parse_bool(dry_run)
    sync_prices = _parse_bool(sync_prices)
    start_batch = cint(start_batch) or 1

    # Count templates - with optional brand filter
    # Include disabled items - they will be synced with active=false
    if brand:
        # Get templates filtered by brand
        template_codes = frappe.get_all(
            "Item",
            filters={"has_variants": 1, "brand": brand},
            pluck="name"
        )
        # Filter to only those synced to Shopware
        total = frappe.db.count("Ecommerce Item", {
            "integration": MODULE_NAME,
            "has_variants": 1,
            "erpnext_item_code": ("in", template_codes)
        }) if template_codes else 0
    else:
        total = frappe.db.count("Ecommerce Item", {
            "integration": MODULE_NAME,
            "has_variants": 1
        })

    if total == 0:
        return {
            "success": False,
            "message": "No template products found to sync"
        }

    job_name = f"shopware6_force_variant_sync_{now()}"

    frappe.enqueue(
        "ecommerce_integrations.shopware6.export.reconciliation._run_force_variant_sync_batched",
        queue="long",
        timeout=14400,  # 4 hours for large syncs
        job_name=job_name,
        batch_size=batch_size,
        total=total,
        dry_run=dry_run,
        sync_prices=sync_prices,
        price_list=price_list,
        brand=brand,
        start_batch=start_batch,
    )

    brand_info = f" (brand: {brand})" if brand else ""
    start_info = f" starting from batch {start_batch}" if start_batch > 1 else ""
    create_shopware_log(
        status="Info",
        method="force_sync_all_variants",
        message=f"Force variant sync enqueued for {total} templates{brand_info} (batch: {batch_size}, prices: {sync_prices}){start_info}"
                + (" DRY RUN" if dry_run else ""),
    )

    return {
        "success": True,
        "job_name": job_name,
        "message": f"Force variant sync enqueued for {total} templates{brand_info} (batch size: {batch_size}){start_info}"
                   + (" - DRY RUN" if dry_run else ""),
    }


def _run_force_variant_sync_batched(
    batch_size: int,
    total: int,
    dry_run: bool = False,
    sync_prices: bool = True,
    price_list: str = "Standard-Vertrieb",
    brand: str = None,
    start_batch: int = 1
):
    """
    Run force variant sync in batches.

    Internal function called by background job.
    Uses BatchProductUploader for better performance and full field support (deliveryTime, etc).

    Args:
        batch_size: Templates per batch
        total: Total number of templates
        dry_run: Preview mode
        sync_prices: Also force sync prices
        price_list: Price list for price sync
        brand: Filter by brand
        start_batch: Start from this batch number (1-based, for resuming interrupted syncs)
    """
    import time
    from ecommerce_integrations.shopware6.export.batch_uploader import BatchProductUploader
    from ecommerce_integrations.shopware6.export.template_handler import (
        upload_template_item_to_shopware,
        clear_product_configurator_settings,
    )
    from ecommerce_integrations.shopware6.export.image_handler import sync_product_images_to_shopware
    from ecommerce_integrations.shopware6.export.utils import get_shopware_document_id as get_sw_id

    # Get chunk settings (configurable via Shopware Setting)
    variant_chunk_size, variant_chunk_delay = _get_variant_chunk_settings()

    stats = {"templates_processed": 0, "variants_deleted": 0, "variants_synced": 0, "prices_synced": 0, "images_synced": 0, "failed": 0, "errors": []}
    
    def _ensure_db_connection():
        """Ensure database connection is active."""
        try:
            frappe.db.sql("SELECT 1")
        except Exception:
            try:
                frappe.connect()
                _logger.info("[Force Variant Sync] Reconnected to database")
            except Exception as e:
                _logger.warning(f"[Force Variant Sync] Reconnect attempt failed: {e}")

    def _safe_commit():
        """Commit with reconnect on failure."""
        try:
            frappe.db.commit()
        except Exception as commit_error:
            _logger.warning(f"[Force Variant Sync] Commit failed, trying reconnect: {commit_error}")
            try:
                frappe.connect()
                frappe.db.commit()
            except Exception:
                pass

    def _get_fresh_client():
        """Get a fresh Shopware client, handling connection errors."""
        try:
            return get_shopware_client()
        except Exception as e:
            _logger.error(f"[Force Variant Sync] Could not get Shopware client: {e}")
            return None

    try:
        client = _get_fresh_client()
        if not client:
            create_shopware_log(
                status="Error",
                method="force_sync_all_variants",
                message="Could not connect to Shopware"
            )
            return
        
        # Initialize BatchProductUploader once for better performance
        uploader = BatchProductUploader()

        # Get templates - with optional brand filter
        _ensure_db_connection()
        
        # Include disabled items - they will be synced with active=false
        if brand:
            # Get template codes filtered by brand
            template_codes = frappe.get_all(
                "Item",
                filters={"has_variants": 1, "brand": brand},
                pluck="name"
            )
            template_ecom_items = frappe.get_all(
                "Ecommerce Item",
                filters={
                    "integration": MODULE_NAME,
                    "has_variants": 1,
                    "erpnext_item_code": ("in", template_codes)
                },
                fields=["erpnext_item_code", "integration_item_code"]
            ) if template_codes else []
        else:
            template_ecom_items = frappe.get_all(
                "Ecommerce Item",
                filters={"integration": MODULE_NAME, "has_variants": 1},
                fields=["erpnext_item_code", "integration_item_code"]
            )

        num_batches = (len(template_ecom_items) + batch_size - 1) // batch_size

        # Validate start_batch
        start_batch = max(1, start_batch)
        start_info = f" (starting from batch {start_batch})" if start_batch > 1 else ""

        create_shopware_log(
            status="Info",
            method="force_sync_all_variants",
            message=f"Starting force variant sync for {len(template_ecom_items)} templates in {num_batches} batches{start_info}"
                    + (" DRY RUN" if dry_run else "")
        )

        for batch_num, batch in enumerate(create_batch(template_ecom_items, batch_size)):
            # Skip batches before start_batch (batch_num is 0-based, start_batch is 1-based)
            if batch_num + 1 < start_batch:
                continue

            # Log batch progress (visible in worker logs)
            print(f"[Force Variant Sync] === BATCH {batch_num + 1}/{num_batches} ===", flush=True)

            # Ensure connections are active at start of each batch
            _ensure_db_connection()
            
            # Refresh Shopware client periodically to avoid token expiry
            if batch_num > 0 and batch_num % 5 == 0:
                client = _get_fresh_client()
                if not client:
                    _logger.error("[Force Variant Sync] Lost Shopware connection, aborting")
                    break
            
            for ecom_item in batch:
                template_code = ecom_item.erpnext_item_code
                parent_shopware_id = ecom_item.integration_item_code

                try:
                    # Get ERPNext variants (including disabled - they sync as active=false)
                    erpnext_variants = frappe.get_all(
                        "Item",
                        filters={"variant_of": template_code},
                        pluck="name"
                    )

                    if not erpnext_variants:
                        continue

                    # Get Shopware variants
                    try:
                        response = client.request_post("search/product", {
                            "filter": [{"type": "equals", "field": "parentId", "value": parent_shopware_id}],
                            "limit": 500
                        })
                        shopware_variants = response.get("data", [])
                    except Exception:
                        shopware_variants = []

                    # Delete variants (batch delete for performance)
                    # For large templates, process in chunks to avoid DB overload
                    is_large_template = len(erpnext_variants) > variant_chunk_size
                    if is_large_template:
                        _logger.info(f"[Force Variant Sync] Large template {template_code}: {len(erpnext_variants)} variants - processing in chunks of {variant_chunk_size}")

                    if not dry_run:
                        if shopware_variants:
                            variant_ids = [v.get("id") for v in shopware_variants if v.get("id")]
                            # Log which template we're about to process (visible in worker logs)
                            print(f"[Force Variant Sync] Processing {template_code}: deleting {len(variant_ids)} variants from Shopware", flush=True)
                            # Delete in chunks for large templates
                            if is_large_template:
                                for chunk_idx in range(0, len(variant_ids), variant_chunk_size):
                                    chunk = variant_ids[chunk_idx:chunk_idx + variant_chunk_size]
                                    deleted = _batch_delete_products(client, chunk)
                                    stats["variants_deleted"] += deleted
                                    if chunk_idx + variant_chunk_size < len(variant_ids):
                                        _logger.info(f"[Force Variant Sync] Deleted chunk {chunk_idx // variant_chunk_size + 1}, waiting {variant_chunk_delay}s...")
                                        time.sleep(variant_chunk_delay)
                            else:
                                deleted = _batch_delete_products(client, variant_ids)
                                stats["variants_deleted"] += deleted

                        clear_product_configurator_settings(client, parent_shopware_id)

                        frappe.db.delete("Ecommerce Item", {
                            "integration": MODULE_NAME,
                            "variant_of": template_code
                        })

                        # Re-sync template
                        template_item = frappe.get_doc("Item", template_code)
                        upload_template_item_to_shopware(client, template_item)
                    else:
                        stats["variants_deleted"] += len(shopware_variants)

                    # Re-upload variants using BatchProductUploader for full field support (deliveryTime, etc)
                    # For large templates, process in chunks with delays
                    if not dry_run and erpnext_variants:
                        if is_large_template:
                            # Process large templates in chunks
                            for chunk_idx in range(0, len(erpnext_variants), variant_chunk_size):
                                chunk = erpnext_variants[chunk_idx:chunk_idx + variant_chunk_size]
                                chunk_num = chunk_idx // variant_chunk_size + 1
                                total_chunks = (len(erpnext_variants) + variant_chunk_size - 1) // variant_chunk_size

                                _logger.info(f"[Force Variant Sync] {template_code}: Uploading chunk {chunk_num}/{total_chunks} ({len(chunk)} variants)")

                                result = uploader.upload_variants(chunk, skip_images=True)
                                stats["variants_synced"] += result.success
                                stats["prices_synced"] += result.success
                                stats["failed"] += result.failed

                                # Sync images for this chunk
                                for variant_code in chunk:
                                    try:
                                        variant_shopware_id = get_sw_id("Item", variant_code)
                                        if variant_shopware_id:
                                            variant_item = frappe.get_doc("Item", variant_code)
                                            sync_product_images_to_shopware(client, variant_item, variant_shopware_id)
                                            stats["images_synced"] += 1
                                    except Exception as img_err:
                                        _logger.warning(f"[Force Variant Sync] Image sync failed for {variant_code}: {img_err}")

                                # Wait between chunks to let DB recover
                                if chunk_idx + variant_chunk_size < len(erpnext_variants):
                                    _logger.info(f"[Force Variant Sync] Chunk {chunk_num} done, waiting {variant_chunk_delay}s for DB recovery...")
                                    time.sleep(variant_chunk_delay)
                                    _safe_commit()  # Commit after each chunk
                        else:
                            # Normal processing for small templates
                            result = uploader.upload_variants(erpnext_variants, skip_images=True)
                            stats["variants_synced"] += result.success
                            stats["prices_synced"] += result.success
                            stats["failed"] += result.failed

                            # Sync images for uploaded variants
                            for variant_code in erpnext_variants:
                                try:
                                    variant_shopware_id = get_sw_id("Item", variant_code)
                                    if variant_shopware_id:
                                        variant_item = frappe.get_doc("Item", variant_code)
                                        sync_product_images_to_shopware(client, variant_item, variant_shopware_id)
                                        stats["images_synced"] += 1
                                except Exception as img_err:
                                    _logger.warning(f"[Force Variant Sync] Image sync failed for {variant_code}: {img_err}")
                    else:
                        stats["variants_synced"] += len(erpnext_variants)

                    stats["templates_processed"] += 1

                except Exception as e:
                    stats["failed"] += 1
                    error_msg = f"{template_code}: {str(e)[:100]}"
                    if len(stats["errors"]) < 20:
                        stats["errors"].append(error_msg)
                    _logger.warning(f"[Force Variant Sync] Error for {template_code}: {e}")

                    # Check for DB connection error and reconnect
                    if "InterfaceError" in str(type(e).__name__) or "(0, '')" in str(e):
                        _ensure_db_connection()

            _safe_commit()

            pct = round(stats["templates_processed"] / total * 100, 1) if total > 0 else 0
            create_shopware_log(
                status="Success",
                method="force_sync_all_variants",
                message=f"Batch {batch_num + 1}/{num_batches} ({pct}%) - "
                        f"Templates: {stats['templates_processed']}, "
                        f"Deleted: {stats['variants_deleted']}, Synced: {stats['variants_synced']}, "
                        f"Prices: {stats['prices_synced']}, Images: {stats['images_synced']}",
            )

        create_shopware_log(
            status="Success",
            method="force_sync_all_variants",
            message=f"COMPLETED - Templates: {stats['templates_processed']}, "
                    f"Variants deleted: {stats['variants_deleted']}, "
                    f"Variants synced: {stats['variants_synced']}, "
                    f"Prices: {stats['prices_synced']}, Images: {stats['images_synced']}, Failed: {stats['failed']}",
        )
    
    except Exception as outer_error:
        # Catch any unhandled exception at the outer level
        _logger.error(f"[Force Variant Sync] FATAL ERROR: {outer_error}")
        try:
            create_shopware_log(
                status="Error",
                method="force_sync_all_variants",
                message=f"ABORTED with error: {str(outer_error)[:500]}. "
                        f"Progress: {stats['templates_processed']} templates, "
                        f"{stats['variants_synced']} variants synced, {stats['failed']} failed",
            )
        except Exception:
            pass  # If even logging fails, don't crash


# =============================================================================
# UTILITIES
# =============================================================================

def _parse_bool(value) -> bool:
    """Parse string/bool to boolean."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in ("true", "1", "yes")
    return bool(value)


def _notify_user(message: str, indicator: str = "blue"):
    """Send realtime notification to user."""
    frappe.publish_realtime(
        "msgprint",
        {"message": message, "indicator": indicator},
        user=frappe.session.user
    )
