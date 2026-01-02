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

from typing import Any, Dict, List, Optional, Tuple
from frappe.utils import flt, now, cint, create_batch

import frappe
from frappe import _

from ecommerce_integrations.shopware6.connection import (
    temp_shopware_session,
    get_shopware_client,
)
from ecommerce_integrations.shopware6.constants import (
    MODULE_NAME,
    SETTING_DOCTYPE,
    ITEM_SELLING_RATE_FIELD,
)
from ecommerce_integrations.shopware6.utils import create_shopware_log


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
    dry_run: bool = False
) -> Dict[str, Any]:
    """
    Sync ALL Item Groups under a root category to Shopware.

    Processes in tree order (parent before children) using lft ordering.

    Args:
        client: Shopware API client (injected by decorator)
        root_category: Root category (defaults to setting)
        skip_root: If True, don't sync the root category itself
        sync_empty_categories: If True, sync categories even without products
        dry_run: If True, only report what would be synced

    Returns:
        dict: Sync results with statistics
    """
    from ecommerce_integrations.shopware6.export.category_handler import sync_category_hierarchy

    # Parse string parameters from frontend
    skip_root = _parse_bool(skip_root)
    sync_empty_categories = _parse_bool(sync_empty_categories)
    dry_run = _parse_bool(dry_run)

    setting = frappe.get_cached_doc(SETTING_DOCTYPE)
    if not setting.is_enabled():
        return {"success": False, "message": "Shopware integration is not enabled"}

    # Use setting default if not provided
    if not root_category:
        root_category = getattr(setting, 'category_sync_root', 'Produkte') or 'Produkte'

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
    if not sync_empty_categories:
        categories_with_products = set(frappe.get_all(
            "Item", filters={"disabled": 0}, pluck="item_group"
        ))
        categories = [c for c in categories if c.name in categories_with_products]

    stats = {"synced": 0, "skipped": 0, "errors": [], "total": len(categories)}

    frappe.logger().info(
        f"Category Sync: Processing {len(categories)} categories from '{root_category}'"
    )

    for cat in categories:
        if dry_run:
            stats["synced"] += 1
            continue

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
            frappe.log_error(f"Category sync failed: {cat.name}: {e}", "Shopware Category Sync")

        # Commit periodically
        if stats["synced"] % 20 == 0:
            frappe.db.commit()

    frappe.db.commit()

    create_shopware_log(
        status="Success" if not stats["errors"] else "Partial",
        message=f"Category sync: {stats['synced']}/{stats['total']} synced, {len(stats['errors'])} errors",
        method="sync_all_categories_to_shopware"
    )

    return {
        "success": True,
        "dry_run": dry_run,
        "statistics": stats,
        "message": f"Synced {stats['synced']}/{stats['total']} categories" + (" (DRY RUN)" if dry_run else "")
    }


# =============================================================================
# PRODUCT RECONCILIATION
# =============================================================================

def get_shopware_products_batch(
    client,
    product_ids: List[str],
    include_categories: bool = True
) -> Dict[str, Dict[str, Any]]:
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
        frappe.log_error(f"Batch product fetch failed: {e}", "Shopware Reconciliation")
        return {}


def compare_item_with_shopware(
    erpnext_item,
    shopware_data: Dict[str, Any],
    compare_categories: bool = True
) -> Dict[str, Any]:
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

    frappe.logger().info(f"Reconciliation: Checking {len(ecom_items)} linked items")

    # Process in batches - get batch size from settings
    batch_size = cint(getattr(setting, 'product_batch_size', 50)) or 50
    for batch_start in range(0, len(ecom_items), batch_size):
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

        # Commit periodically
        if stats["total_checked"] % 20 == 0:
            frappe.db.commit()

    # Optionally sync unlinked items
    if include_unlinked and not dry_run:
        synced_codes = [e.erpnext_item_code for e in ecom_items]
        unlinked = frappe.get_all(
            "Item",
            filters={
                "disabled": 0, "has_variants": 0,
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
) -> Dict[str, Any]:
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

@frappe.whitelist()
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
    sync_mehrpreis: bool = True
) -> Dict[str, Any]:
    """
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
        sync_mehrpreis: Sync Mehrpreis properties for items with is_sales_item=0

    Returns:
        Combined results from category and product sync
    """
    # Parse parameters
    dry_run = _parse_bool(dry_run)
    sync_images = _parse_bool(sync_images)
    include_unlinked = _parse_bool(include_unlinked)
    skip_root_category = _parse_bool(skip_root_category)
    sync_empty_categories = _parse_bool(sync_empty_categories)
    cleanup_orphaned_categories = _parse_bool(cleanup_orphaned_categories)
    cleanup_orphaned_variants = _parse_bool(cleanup_orphaned_variants)
    sync_mehrpreis = _parse_bool(sync_mehrpreis)

    setting = frappe.get_cached_doc(SETTING_DOCTYPE)
    if not category_root:
        category_root = getattr(setting, 'category_sync_root', 'Produkte') or 'Produkte'

    results = {
        "success": True,
        "dry_run": dry_run,
        "category_sync": None,
        "product_sync": None,
        "category_cleanup": None,
        "variant_cleanup": None,
        "mehrpreis_sync": None
    }

    frappe.logger().info(
        f"Full Reconciliation: Starting (categories: {category_root}, products: {limit})"
    )

    # Phase 0: Sync Mehrpreis properties (before product sync)
    if sync_mehrpreis and not dry_run:
        from ecommerce_integrations.shopware6.export.property_handler import sync_mehrpreis_properties_batch
        _notify_user("Phase 0/5: Syncing Mehrpreis properties...", "blue")
        results["mehrpreis_sync"] = sync_mehrpreis_properties_batch()

    # Phase 1: Sync categories
    _notify_user("Phase 1/5: Syncing categories...", "blue")
    results["category_sync"] = sync_all_categories_to_shopware(
        root_category=category_root,
        skip_root=skip_root_category,
        sync_empty_categories=sync_empty_categories,
        dry_run=dry_run
    )

    # Phase 2: Sync products
    _notify_user("Phase 2/5: Syncing products...", "blue")
    results["product_sync"] = reconcile_erpnext_with_shopware(
        limit=cint(limit),
        dry_run=dry_run,
        sync_images=sync_images,
        include_unlinked=include_unlinked,
        compare_categories=True
    )

    # Phase 3: Cleanup orphaned variants
    if cleanup_orphaned_variants and not dry_run:
        _notify_user("Phase 3/5: Cleaning up orphaned variants...", "blue")
        results["variant_cleanup"] = _cleanup_all_orphaned_variants()

    # Phase 4: Cleanup orphaned categories
    if cleanup_orphaned_categories:
        _notify_user("Phase 4/5: Cleaning up orphaned categories...", "blue")
        results["category_cleanup"] = cleanup_orphaned_shopware_categories(
            root_category=category_root,
            dry_run=dry_run
        )

    # Build summary message
    cat_stats = results["category_sync"].get("statistics", {})
    prod_stats = results["product_sync"].get("statistics", {})

    message_parts = []

    # Mehrpreis sync stats
    if results.get("mehrpreis_sync"):
        mehrpreis_stats = results["mehrpreis_sync"]
        message_parts.append(f"Mehrpreis: {mehrpreis_stats.get('added', 0)} added")

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
def _cleanup_all_orphaned_variants(client) -> Dict[str, Any]:
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

    frappe.logger().info(f"[Variant Cleanup] Checking {len(template_links)} templates")

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

            # Commit periodically
            if stats["templates_checked"] % 20 == 0:
                frappe.db.commit()

        except Exception as e:
            stats["errors"].append({
                "template": link.erpnext_item_code,
                "error": str(e)[:100]
            })

    frappe.db.commit()

    frappe.logger().info(
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
) -> Dict[str, Any]:
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
        root_category = getattr(setting, 'category_sync_root', 'Produkte') or 'Produkte'

    # Get ERPNext categories under root
    root_info = frappe.db.get_value("Item Group", root_category, ["lft", "rgt"], as_dict=True)
    if not root_info:
        return {"success": False, "message": f"Root category '{root_category}' not found in ERPNext"}

    erpnext_categories = set(frappe.get_all(
        "Item Group",
        filters=[["lft", ">=", root_info.lft], ["rgt", "<=", root_info.rgt]],
        pluck="name"
    )) - {"All Item Groups", "Alle Artikelgruppen"}

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
    orphaned = [
        {"id": c.get("id"), "name": c.get("name"), "parent_id": c.get("parentId")}
        for c in all_shopware_cats
        if c.get("name") and c.get("name") not in erpnext_categories
        and c.get("id") != shopware_root_id  # Never delete the root itself
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

        for cat in orphaned:
            try:
                client.request_delete(f"category/{cat['id']}")
                stats["deleted"] += 1
            except Exception as e:
                stats["failed"] += 1
                stats["errors"].append({"category": cat["name"], "error": str(e)[:100]})

    return {
        "success": True,
        "dry_run": dry_run,
        "statistics": stats,
        "orphaned_categories": [c["name"] for c in orphaned[:20]],
        "message": f"Found {len(orphaned)} orphaned under '{root_category}', deleted {stats['deleted']}" + (" (DRY RUN)" if dry_run else "")
    }


# =============================================================================
# BACKGROUND JOB SUPPORT
# =============================================================================

RECONCILIATION_JOB_NAME = "shopware6.reconciliation.full"


@frappe.whitelist()
def enqueue_full_reconciliation(
    batch_size: int = 50,
    sync_images: bool = True,
    compare_categories: bool = True
) -> Dict[str, Any]:
    """
    Enqueue full reconciliation to run in background.

    Uses batch processing with commits to avoid transaction errors.

    Args:
        batch_size: Items per batch
        sync_images: Sync images for changed items
        compare_categories: Compare categories

    Returns:
        Job enqueue status
    """
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
    # Parse parameters
    dry_run = _parse_bool(dry_run)
    sync_images = _parse_bool(sync_images)
    include_unlinked = _parse_bool(include_unlinked)
    cleanup_orphaned_categories = _parse_bool(cleanup_orphaned_categories)

    setting = frappe.get_cached_doc(SETTING_DOCTYPE)

    if category_root is None:
        category_root = getattr(setting, 'category_sync_root', 'Produkte') or 'Produkte'
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
        frappe.logger().error("Shopware integration not enabled")
        return

    # Get ALL linked items
    ecom_items = frappe.get_all(
        "Ecommerce Item",
        filters={"integration": MODULE_NAME},
        fields=["erpnext_item_code", "integration_item_code"],
        order_by="modified desc"
    )

    total_items = len(ecom_items)
    frappe.logger().info(f"[Reconciliation] Starting for {total_items} items")

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
                    frappe.logger().warning(f"[Reconciliation] Error: {item_code}: {e}")

            frappe.db.commit()

            # Log progress every 5 batches
            if batch_num % 5 == 0:
                pct = round(stats["processed"] / total_items * 100, 1)
                frappe.logger().info(
                    f"[Reconciliation] {pct}% - Synced: {stats['synced']}, "
                    f"In Sync: {stats['in_sync']}, Errors: {stats['errors']}"
                )

        except Exception as e:
            frappe.logger().error(f"[Reconciliation] Batch {batch_num} failed: {e}")
            frappe.db.rollback()

    stats["end_time"] = now()
    frappe.logger().info(
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
# FORCE IMAGE SYNC
# =============================================================================

@frappe.whitelist()
@temp_shopware_session
def force_sync_all_images(
    client,
    limit: int = 100,
    offset: int = 0,
) -> Dict[str, Any]:
    """
    Force re-sync all product images to Shopware.

    This will:
    1. Clear the image hash cache
    2. Delete existing images in Shopware
    3. Re-upload all images from ERPNext

    Args:
        limit: Maximum number of products to process
        offset: Starting offset for pagination

    Returns:
        Dict with sync statistics
    """
    from ecommerce_integrations.shopware6.export.image_handler import sync_product_images_to_shopware
    from ecommerce_integrations.shopware6.base.cache_manager import get_cache

    limit = cint(limit) or 100
    offset = cint(offset) or 0

    stats = {
        "processed": 0,
        "synced": 0,
        "failed": 0,
        "errors": [],
    }

    # Get all products with Shopware ID that have images
    # Join with Item to filter only items with images
    ecommerce_items = frappe.db.sql("""
        SELECT ei.erpnext_item_code, ei.integration_item_code
        FROM `tabEcommerce Item` ei
        INNER JOIN `tabItem` i ON i.name = ei.erpnext_item_code
        WHERE ei.integration = 'Shopware6'
        AND i.image IS NOT NULL
        AND i.image != ''
        ORDER BY ei.erpnext_item_code
        LIMIT %s OFFSET %s
    """, (limit, offset), as_dict=True)

    cache = get_cache()

    for ei in ecommerce_items:
        item_code = ei.erpnext_item_code
        shopware_id = ei.integration_item_code

        try:
            # Get the Item document
            item = frappe.get_doc("Item", item_code)

            # Clear cached hashes for this item to force re-upload
            cache.clear_image_hashes(item_code)

            # Sync images (this deletes old and uploads new)
            success = sync_product_images_to_shopware(client, item, shopware_id)

            if success:
                stats["synced"] += 1
            else:
                stats["failed"] += 1
                stats["errors"].append({"item_code": item_code, "error": "sync failed"})

            stats["processed"] += 1

            # Commit every 10 items
            if stats["processed"] % 10 == 0:
                frappe.db.commit()
                frappe.logger().info(
                    f"[Force Image Sync] Progress: {stats['processed']}/{len(ecommerce_items)} "
                    f"(synced: {stats['synced']}, failed: {stats['failed']})"
                )

        except Exception as e:
            stats["failed"] += 1
            stats["errors"].append({"item_code": item_code, "error": str(e)})
            stats["processed"] += 1
            frappe.logger().warning(f"[Force Image Sync] Error for {item_code}: {e}")

    frappe.db.commit()

    return {
        "success": True,
        "statistics": stats,
        "message": f"Processed {stats['processed']} items: {stats['synced']} synced, {stats['failed']} failed",
    }


@frappe.whitelist()
def enqueue_force_sync_all_images(
    batch_size: int = 50,
) -> Dict[str, Any]:
    """
    Enqueue force image sync as a background job.

    Processes all products with images in batches.

    Args:
        batch_size: Number of items per batch

    Returns:
        Dict with job info
    """
    # Get batch size from settings if not provided
    if not batch_size:
        setting = frappe.get_cached_doc(SETTING_DOCTYPE)
        batch_size = cint(getattr(setting, 'image_batch_size', 50)) or 50
    batch_size = cint(batch_size) or 50

    # Count total items with images
    total = frappe.db.sql("""
        SELECT COUNT(*) FROM `tabEcommerce Item` ei
        INNER JOIN `tabItem` i ON i.name = ei.erpnext_item_code
        WHERE ei.integration = 'Shopware6'
        AND i.image IS NOT NULL AND i.image != ''
    """)[0][0]

    job_name = f"shopware6_force_image_sync_{now()}"

    frappe.enqueue(
        "ecommerce_integrations.shopware6.export.reconciliation._run_force_image_sync_batched",
        queue="long",
        job_name=job_name,
        timeout=3600 * 4,  # 4 hours
        batch_size=batch_size,
        total=total,
    )

    return {
        "success": True,
        "message": f"Force image sync enqueued for {total} products (batch size: {batch_size})",
        "job_name": job_name,
    }


def _run_force_image_sync_batched(batch_size: int, total: int):
    """
    Run force image sync in batches.

    Args:
        batch_size: Number of items per batch
        total: Total items to process
    """
    from ecommerce_integrations.shopware6.export.image_handler import sync_product_images_to_shopware
    from ecommerce_integrations.shopware6.base.cache_manager import get_cache

    client = get_shopware_client()
    if not client:
        create_shopware_log(
            status="Error",
            method="force_sync_all_images",
            message="No Shopware client available",
        )
        return

    cache = get_cache()
    stats = {"processed": 0, "synced": 0, "failed": 0}
    num_batches = (total + batch_size - 1) // batch_size

    create_shopware_log(
        status="Queued",
        method="force_sync_all_images",
        message=f"Starting image sync for {total} products in {num_batches} batches (batch size: {batch_size})",
    )

    for batch_num in range(num_batches):
        offset = batch_num * batch_size

        items = frappe.db.sql("""
            SELECT ei.erpnext_item_code, ei.integration_item_code
            FROM `tabEcommerce Item` ei
            INNER JOIN `tabItem` i ON i.name = ei.erpnext_item_code
            WHERE ei.integration = 'Shopware6'
            AND i.image IS NOT NULL AND i.image != ''
            ORDER BY ei.erpnext_item_code
            LIMIT %s OFFSET %s
        """, (batch_size, offset), as_dict=True)

        for ei in items:
            item_code = ei.erpnext_item_code
            shopware_id = ei.integration_item_code

            try:
                item = frappe.get_doc("Item", item_code)
                cache.clear_image_hashes(item_code)
                success = sync_product_images_to_shopware(client, item, shopware_id)

                if success:
                    stats["synced"] += 1
                else:
                    stats["failed"] += 1

                stats["processed"] += 1

            except Exception as e:
                stats["failed"] += 1
                stats["processed"] += 1
                frappe.log_error(f"Force Image Sync error for {item_code}: {e}")

        frappe.db.commit()

        pct = round(stats["processed"] / total * 100, 1)
        create_shopware_log(
            status="Success",
            method="force_sync_all_images",
            message=f"Batch {batch_num + 1}/{num_batches} ({pct}%) - Synced: {stats['synced']}, Failed: {stats['failed']}",
        )

    create_shopware_log(
        status="Success",
        method="force_sync_all_images",
        message=f"COMPLETED - Total synced: {stats['synced']}, Failed: {stats['failed']}",
    )


# =============================================================================
# FORCE VARIANT SYNC
# =============================================================================

@frappe.whitelist()
@temp_shopware_session
def force_sync_all_variants(
    client,
    limit: int = 0,
    dry_run: bool = False
) -> Dict[str, Any]:
    """
    Force re-sync all variant products to Shopware.

    This function:
    1. Finds all template products (has_variants=1) synced to Shopware
    2. For each template: deletes ALL variants in Shopware
    3. Re-uploads all variants from ERPNext

    Args:
        client: Shopware API client (injected by decorator)
        limit: Max products to process (0 = all)
        dry_run: Preview changes without applying

    Returns:
        Dict with sync statistics
    """
    from ecommerce_integrations.shopware6.export.variant_handler import upload_variant_item_to_shopware
    from ecommerce_integrations.shopware6.export.template_handler import (
        upload_template_item_to_shopware,
        clear_product_configurator_settings,
    )

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
        message=f"Starting force variant sync (limit={limit}, dry_run={dry_run})"
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
    frappe.logger().info(f"[Force Variant Sync] Found {total} template products")

    for idx, ecom_item in enumerate(template_ecom_items):
        template_code = ecom_item.erpnext_item_code
        parent_shopware_id = ecom_item.integration_item_code

        try:
            # Get ERPNext variants
            erpnext_variants = frappe.get_all(
                "Item",
                filters={"variant_of": template_code, "disabled": 0},
                pluck="name"
            )

            if not erpnext_variants:
                frappe.logger().info(f"[Force Variant Sync] {template_code} has no variants, skipping")
                continue

            # Step 1: Get all Shopware variants for this parent
            try:
                response = client.request_post("search/product", {
                    "filter": [{"type": "equals", "field": "parentId", "value": parent_shopware_id}],
                    "limit": 500
                })
                shopware_variants = response.get("data", [])
            except Exception as e:
                frappe.logger().warning(f"[Force Variant Sync] Could not fetch variants for {template_code}: {e}")
                shopware_variants = []

            # Step 2: Delete ALL variants in Shopware
            if not dry_run:
                for sw_variant in shopware_variants:
                    sw_variant_id = sw_variant.get("id")
                    try:
                        client.request_delete(f"product/{sw_variant_id}")
                        stats["variants_deleted"] += 1
                    except Exception as e:
                        frappe.logger().warning(f"[Force Variant Sync] Could not delete variant {sw_variant_id}: {e}")

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

            # Step 4: Re-upload all variants from ERPNext
            for variant_code in erpnext_variants:
                try:
                    if not dry_run:
                        variant_item = frappe.get_doc("Item", variant_code)
                        result = upload_variant_item_to_shopware(client, variant_item)
                        if result:
                            stats["variants_synced"] += 1
                        else:
                            stats["failed"] += 1
                    else:
                        stats["variants_synced"] += 1
                except Exception as e:
                    stats["failed"] += 1
                    stats["errors"].append(f"{variant_code}: {str(e)[:100]}")

            stats["templates_processed"] += 1

            if (idx + 1) % 5 == 0:
                frappe.logger().info(
                    f"[Force Variant Sync] Progress: {idx + 1}/{total} templates, "
                    f"deleted: {stats['variants_deleted']}, synced: {stats['variants_synced']}"
                )
                if not dry_run:
                    frappe.db.commit()

        except Exception as e:
            stats["failed"] += 1
            stats["errors"].append(f"{template_code}: {str(e)[:100]}")
            frappe.logger().warning(f"[Force Variant Sync] Error for {template_code}: {e}")

    create_shopware_log(
        status="Success",
        method="force_sync_all_variants",
        message=f"Force variant sync completed: {stats['templates_processed']} templates, "
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
    price_list: str = "Standard-Vertrieb"
) -> Dict[str, Any]:
    """
    Force re-sync all variants for a SINGLE template product.

    Deletes all variants in Shopware and re-uploads from ERPNext.
    Also syncs prices after variant creation.

    Args:
        client: Shopware API client (injected)
        template_item_code: ERPNext template item code (e.g. '4702A-Parent')
        dry_run: Preview mode
        sync_prices: Also force sync prices after variant creation (default: True)
        price_list: Price list to use for price sync

    Returns:
        Dict with sync statistics
    """
    from ecommerce_integrations.shopware6.export.variant_handler import upload_variant_item_to_shopware
    from ecommerce_integrations.shopware6.export.template_handler import (
        upload_template_item_to_shopware,
        clear_product_configurator_settings,
    )
    from ecommerce_integrations.shopware6.export.utils import get_shopware_document_id

    stats = {
        "template": template_item_code,
        "variants_deleted": 0,
        "variants_synced": 0,
        "prices_synced": 0,
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

    frappe.logger().info(f"[Force Single Variant Sync] Starting for {template_item_code} (Shopware ID: {parent_shopware_id})")

    try:
        # Get ERPNext variants
        erpnext_variants = frappe.get_all(
            "Item",
            filters={"variant_of": template_item_code, "disabled": 0},
            pluck="name"
        )
        frappe.logger().info(f"[Force Single Variant Sync] Found {len(erpnext_variants)} ERPNext variants")

        # Get Shopware variants
        try:
            response = client.request_post("search/product", {
                "filter": [{"type": "equals", "field": "parentId", "value": parent_shopware_id}],
                "limit": 500
            })
            shopware_variants = response.get("data", [])
            frappe.logger().info(f"[Force Single Variant Sync] Found {len(shopware_variants)} Shopware variants")
        except Exception as e:
            frappe.logger().warning(f"Could not fetch Shopware variants: {e}")
            shopware_variants = []

        # Step 1: Delete ALL variants in Shopware
        if not dry_run:
            for sw_variant in shopware_variants:
                sw_variant_id = sw_variant.get("id")
                product_number = sw_variant.get("productNumber", "")
                try:
                    client.request_delete(f"product/{sw_variant_id}")
                    stats["variants_deleted"] += 1
                    frappe.logger().info(f"Deleted variant {product_number} ({sw_variant_id})")
                except Exception as e:
                    frappe.logger().warning(f"Could not delete variant {sw_variant_id}: {e}")

            # Clear configurator settings from parent
            frappe.logger().info("Clearing configuratorSettings from parent...")
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
            frappe.logger().info("Re-syncing template product...")
            template_item = frappe.get_doc("Item", template_item_code)
            upload_template_item_to_shopware(client, template_item)

        # Step 3: Re-upload all variants from ERPNext with full Multi-Channel support
        frappe.logger().info(f"Re-uploading {len(erpnext_variants)} variants...")
        for variant_code in erpnext_variants:
            try:
                if not dry_run:
                    variant_item = frappe.get_doc("Item", variant_code)
                    result = upload_variant_item_to_shopware(client, variant_item)
                    if result:
                        stats["variants_synced"] += 1
                        frappe.logger().info(f"Synced variant {variant_code}")
                    else:
                        stats["failed"] += 1
                        stats["errors"].append(f"{variant_code}: upload returned None (no valid price?)")
                else:
                    stats["variants_synced"] += 1
            except Exception as e:
                stats["failed"] += 1
                stats["errors"].append(f"{variant_code}: {str(e)[:100]}")
                frappe.logger().warning(f"Failed to sync {variant_code}: {e}")

        if not dry_run:
            frappe.db.commit()

        # Prices are set during variant upload (with Multi-Channel support)
        # No separate price sync needed - upload_variant_item_to_shopware handles it
        stats["prices_synced"] = stats["variants_synced"]

    except Exception as e:
        stats["errors"].append(str(e)[:200])
        frappe.log_error(f"Force single variant sync failed for {template_item_code}: {e}")

    create_shopware_log(
        status="Success" if stats["failed"] == 0 else "Warning",
        method="force_sync_single_template_variants",
        message=f"{template_item_code}: deleted {stats['variants_deleted']}, synced {stats['variants_synced']}, prices {stats['prices_synced']}, failed {stats['failed']}"
    )

    return {
        "success": True,
        "stats": stats,
        "message": f"Template {template_item_code}: {stats['variants_deleted']} deleted, {stats['variants_synced']} synced, {stats['prices_synced']} prices"
    }


@frappe.whitelist()
def enqueue_force_sync_all_variants(
    batch_size: int = 10,
    dry_run: bool = False,
    sync_prices: bool = True,
    price_list: str = "Standard-Vertrieb",
    brand: str = None
) -> Dict[str, Any]:
    """
    Enqueue force variant sync as a background job.

    Args:
        batch_size: Templates per batch
        dry_run: Preview mode
        sync_prices: Also force sync prices (default: True)
        price_list: Price list for price sync
        brand: Filter by brand (e.g. 'vendor')

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

    # Count templates - with optional brand filter
    if brand:
        # Get templates filtered by brand
        template_codes = frappe.get_all(
            "Item",
            filters={"has_variants": 1, "brand": brand, "disabled": 0},
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
    )

    brand_info = f" (brand: {brand})" if brand else ""
    create_shopware_log(
        status="Info",
        method="force_sync_all_variants",
        message=f"Force variant sync enqueued for {total} templates{brand_info} (batch: {batch_size}, prices: {sync_prices})"
                + (" DRY RUN" if dry_run else ""),
    )

    return {
        "success": True,
        "job_name": job_name,
        "message": f"Force variant sync enqueued for {total} templates{brand_info} (batch size: {batch_size})"
                   + (" - DRY RUN" if dry_run else ""),
    }


def _run_force_variant_sync_batched(
    batch_size: int,
    total: int,
    dry_run: bool = False,
    sync_prices: bool = True,
    price_list: str = "Standard-Vertrieb",
    brand: str = None
):
    """
    Run force variant sync in batches.

    Internal function called by background job.
    """
    from ecommerce_integrations.shopware6.export.variant_handler import upload_variant_item_to_shopware
    from ecommerce_integrations.shopware6.export.template_handler import (
        upload_template_item_to_shopware,
        clear_product_configurator_settings,
    )

    client = get_shopware_client()
    if not client:
        create_shopware_log(
            status="Error",
            method="force_sync_all_variants",
            message="Could not connect to Shopware"
        )
        return

    stats = {"templates_processed": 0, "variants_deleted": 0, "variants_synced": 0, "prices_synced": 0, "failed": 0}

    # Get templates - with optional brand filter
    if brand:
        # Get template codes filtered by brand
        template_codes = frappe.get_all(
            "Item",
            filters={"has_variants": 1, "brand": brand, "disabled": 0},
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

    create_shopware_log(
        status="Info",
        method="force_sync_all_variants",
        message=f"Starting force variant sync for {len(template_ecom_items)} templates in {num_batches} batches"
                + (" DRY RUN" if dry_run else "")
    )

    for batch_num, batch in enumerate(create_batch(template_ecom_items, batch_size)):
        for ecom_item in batch:
            template_code = ecom_item.erpnext_item_code
            parent_shopware_id = ecom_item.integration_item_code

            try:
                # Get ERPNext variants
                erpnext_variants = frappe.get_all(
                    "Item",
                    filters={"variant_of": template_code, "disabled": 0},
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

                # Delete variants
                if not dry_run:
                    for sw_variant in shopware_variants:
                        try:
                            client.request_delete(f"product/{sw_variant.get('id')}")
                            stats["variants_deleted"] += 1
                        except Exception:
                            pass

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

                # Re-upload variants with full Multi-Channel support
                for variant_code in erpnext_variants:
                    try:
                        if not dry_run:
                            variant_item = frappe.get_doc("Item", variant_code)
                            if upload_variant_item_to_shopware(client, variant_item):
                                stats["variants_synced"] += 1
                                stats["prices_synced"] += 1  # Price is set during upload
                            else:
                                stats["failed"] += 1
                        else:
                            stats["variants_synced"] += 1
                    except Exception:
                        stats["failed"] += 1

                stats["templates_processed"] += 1

            except Exception as e:
                stats["failed"] += 1
                frappe.log_error(f"Force Variant Sync error for {template_code}: {e}")

        frappe.db.commit()

        pct = round(stats["templates_processed"] / total * 100, 1) if total > 0 else 0
        create_shopware_log(
            status="Success",
            method="force_sync_all_variants",
            message=f"Batch {batch_num + 1}/{num_batches} ({pct}%) - "
                    f"Templates: {stats['templates_processed']}, "
                    f"Deleted: {stats['variants_deleted']}, Synced: {stats['variants_synced']}, "
                    f"Prices: {stats['prices_synced']}",
        )

    create_shopware_log(
        status="Success",
        method="force_sync_all_variants",
        message=f"COMPLETED - Templates: {stats['templates_processed']}, "
                f"Variants deleted: {stats['variants_deleted']}, "
                f"Variants synced: {stats['variants_synced']}, "
                f"Prices synced: {stats['prices_synced']}, Failed: {stats['failed']}",
    )


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
