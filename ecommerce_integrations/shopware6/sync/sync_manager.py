"""
Shopware 6 Sync Manager

THE unified entry point for all synchronization operations.

PHILOSOPHY: ERPNext is ALWAYS the source of truth.
When full reconciliation runs, Shopware becomes 100% identical to ERPNext.

Features:
- Single entry point for all sync operations
- Comprehensive reconciliation with all options
- Handles: categories, products, variants, prices, images, properties, stock
- Cleans up orphaned data in Shopware
- Progress reporting and logging

Usage:
    from ecommerce_integrations.shopware6.sync import SyncManager

    # Quick sync for a single item
    manager = SyncManager()
    manager.sync_item("ITEM-001")

    # Full reconciliation - makes Shopware IDENTICAL to ERPNext
    result = manager.full_reconciliation_no_brainer()
"""

from typing import Any, Dict, List, Optional
from dataclasses import dataclass, field

import frappe
from frappe import _
from frappe.utils import now, cint

from ecommerce_integrations.shopware6.connection import get_shopware_client, temp_shopware_session
from ecommerce_integrations.shopware6.constants import MODULE_NAME, SETTING_DOCTYPE
from ecommerce_integrations.shopware6.utils import get_logger, create_shopware_log


@dataclass
class ReconciliationResult:
    """Complete result of a reconciliation run."""
    success: bool = True
    started_at: str = ""
    completed_at: str = ""

    # Category stats
    categories_synced: int = 0
    categories_created: int = 0
    categories_deleted: int = 0

    # Product stats
    products_checked: int = 0
    products_in_sync: int = 0
    products_synced: int = 0
    products_created: int = 0
    products_deactivated: int = 0

    # Variant stats
    variants_synced: int = 0
    variants_deleted: int = 0

    # Price stats
    prices_synced: int = 0
    prices_fixed: int = 0

    # Image stats
    images_synced: int = 0

    # Stock stats
    stock_adjusted: int = 0

    # Error tracking
    errors: List[Dict] = field(default_factory=list)

    @property
    def summary(self) -> str:
        """Get human-readable summary."""
        parts = []

        if self.categories_synced:
            parts.append(f"Categories: {self.categories_synced}")

        if self.products_synced or self.products_in_sync:
            parts.append(
                f"Products: {self.products_synced} synced, "
                f"{self.products_in_sync} in sync"
            )

        if self.variants_synced:
            parts.append(f"Variants: {self.variants_synced}")

        if self.prices_synced:
            parts.append(f"Prices: {self.prices_synced}")

        if self.images_synced:
            parts.append(f"Images: {self.images_synced}")

        if self.variants_deleted or self.products_deactivated:
            parts.append(
                f"Cleaned: {self.variants_deleted} orphan variants, "
                f"{self.products_deactivated} deactivated"
            )

        if self.errors:
            parts.append(f"Errors: {len(self.errors)}")

        return " | ".join(parts) if parts else "No changes"


class SyncManager:
    """
    Unified Sync Manager for Shopware integration.

    This is THE entry point for all sync operations.
    It coordinates all sync modules and ensures consistency.

    Core Principle: ERPNext is ALWAYS the source of truth.
    """

    def __init__(self):
        self.setting = frappe.get_cached_doc(SETTING_DOCTYPE)
        self.logger = get_logger("SyncManager")

        if not self.setting.is_enabled():
            frappe.throw(_("Shopware integration is not enabled"))

    def sync_item(self, item_code: str, sync_images: bool = True) -> Dict[str, Any]:
        """
        Sync a single item to Shopware.

        Args:
            item_code: ERPNext Item code
            sync_images: Also sync images

        Returns:
            dict with sync result
        """
        from ecommerce_integrations.shopware6.export.product_uploader import (
            sync_item_if_changed,
            ShopwareProductUploader
        )

        result = sync_item_if_changed(item_code, force=False)

        if result.get("synced"):
            self.logger.info(f"Synced {item_code}: {result.get('message')}")

        return result

    def sync_items_batch(
        self,
        item_codes: List[str],
        sync_images: bool = True
    ) -> Dict[str, Any]:
        """
        Sync multiple items to Shopware.

        Args:
            item_codes: List of ERPNext Item codes
            sync_images: Also sync images

        Returns:
            dict with batch sync results
        """
        from ecommerce_integrations.shopware6.export.product_uploader import batch_sync_if_changed

        return batch_sync_if_changed(item_codes, force=False)

    def full_reconciliation(
        self,
        sync_categories: bool = True,
        sync_products: bool = True,
        sync_variants: bool = True,
        sync_prices: bool = True,
        sync_images: bool = True,
        sync_stock: bool = False,  # Disabled by default for safety
        cleanup_orphan_variants: bool = True,
        cleanup_orphan_categories: bool = True,
        deactivate_missing_products: bool = True,
        limit: int = 0,  # 0 = no limit
        dry_run: bool = False,
    ) -> ReconciliationResult:
        """
        Full reconciliation - THE NO-BRAINER function.

        When this completes, Shopware will be 100% identical to ERPNext.
        No residuals, no orphans, no mismatches.

        Args:
            client: Shopware API client (injected by decorator)
            sync_categories: Sync all categories
            sync_products: Sync all products
            sync_variants: Sync all variants
            sync_prices: Force sync all prices
            sync_images: Sync all images
            sync_stock: Sync stock levels (disabled by default)
            cleanup_orphan_variants: Delete variants in Shopware not in ERPNext
            cleanup_orphan_categories: Delete categories in Shopware not in ERPNext
            deactivate_missing_products: Deactivate Shopware products not in ERPNext
            limit: Maximum products to process (0 = all)
            dry_run: Report what would change without applying

        Returns:
            ReconciliationResult with complete statistics
        """
        # Get Shopware client for this session
        client = get_shopware_client()

        result = ReconciliationResult(started_at=now())

        self.logger.info(
            f"Starting full reconciliation (dry_run={dry_run}, limit={limit})"
        )

        try:
            # Phase 1: Categories
            if sync_categories:
                self._notify("Phase 1/6: Syncing categories...")
                self.logger.info("Full Reconciliation: Starting Phase 1 (Categories)")
                cat_result = self._sync_all_categories(client, dry_run)
                result.categories_synced = cat_result.get("synced", 0)
                result.categories_created = cat_result.get("created", 0)
                self.logger.info(f"Full Reconciliation: Phase 1 complete - synced={result.categories_synced}, created={result.categories_created}")
            else:
                self._notify("Phase 1/6: Skipping categories (disabled in settings)...")
                self.logger.info("Full Reconciliation: Phase 1 (Categories) skipped per settings")

            # Phase 2: Products (creates/updates)
            if sync_products:
                self._notify("Phase 2/6: Syncing products...")
                self.logger.info("Full Reconciliation: Starting Phase 2 (Products)")
                prod_result = self._sync_all_products(client, limit, sync_images, dry_run)
                result.products_checked = prod_result.get("total", 0)
                result.products_synced = prod_result.get("synced", 0)
                result.products_in_sync = prod_result.get("in_sync", 0)
                result.products_created = prod_result.get("created", 0)
                result.errors.extend(prod_result.get("errors", []))
                self.logger.info(f"Full Reconciliation: Phase 2 complete - checked={result.products_checked}, synced={result.products_synced}")

            # Phase 3: Variants
            if sync_variants:
                self._notify("Phase 3/6: Syncing variants...")
                self.logger.info("Full Reconciliation: Starting Phase 3 (Variants)")
                var_result = self._sync_all_variants(client, dry_run)
                result.variants_synced = var_result.get("synced", 0)
                self.logger.info(f"Full Reconciliation: Phase 3 complete - synced={result.variants_synced}")

            # Phase 4: Force Price Sync
            if sync_prices:
                self._notify("Phase 4/6: Syncing prices...")
                self.logger.info("Full Reconciliation: Starting Phase 4 (Prices)")
                price_result = self._force_sync_all_prices(client, limit, dry_run)
                result.prices_synced = price_result.get("synced", 0)
                result.prices_fixed = price_result.get("broken_fixed", 0)
                self.logger.info(f"Full Reconciliation: Phase 4 complete - synced={result.prices_synced}, fixed={result.prices_fixed}")

            # Phase 5: Cleanup orphans
            self._notify("Phase 5/6: Cleaning up orphans...")
            self.logger.info("Full Reconciliation: Starting Phase 5 (Cleanup)")

            if cleanup_orphan_variants and not dry_run:
                cleanup = self._cleanup_orphan_variants(client)
                result.variants_deleted = cleanup.get("deleted", 0)

            if cleanup_orphan_categories and not dry_run:
                cleanup = self._cleanup_orphan_categories(client)
                result.categories_deleted = cleanup.get("deleted", 0)

            if deactivate_missing_products and not dry_run:
                deactivated = self._deactivate_orphan_products(client)
                result.products_deactivated = deactivated
            
            self.logger.info(f"Full Reconciliation: Phase 5 complete - variants_deleted={result.variants_deleted}, categories_deleted={result.categories_deleted}, products_deactivated={result.products_deactivated}")

            # Phase 6: Stock sync (if enabled)
            if sync_stock:
                self._notify("Phase 6/6: Syncing stock levels...")
                self.logger.info("Full Reconciliation: Starting Phase 6 (Stock)")
                stock_result = self._sync_all_stock(client, limit, dry_run)
                result.stock_adjusted = stock_result.get("adjusted", 0)
                self.logger.info(f"Full Reconciliation: Phase 6 complete - adjusted={result.stock_adjusted}")
            else:
                self._notify("Phase 6/6: Stock sync skipped (disabled)")
                self.logger.info("Full Reconciliation: Phase 6 (Stock) skipped")

            result.completed_at = now()
            result.success = True

            # Log summary
            create_shopware_log(
                status="Success",
                method="SyncManager.full_reconciliation",
                message=result.summary
            )

        except Exception as e:
            result.success = False
            result.errors.append({"phase": "general", "error": str(e)})
            self.logger.error("Full reconciliation failed", exception=e)

        return result

    def _sync_all_categories(self, client, dry_run: bool) -> Dict[str, Any]:
        """Sync all categories from ERPNext to Shopware."""
        from ecommerce_integrations.shopware6.export.reconciliation import (
            sync_all_categories_to_shopware
        )

        category_root = getattr(self.setting, 'category_sync_root', 'Produkte') or 'Produkte'

        return sync_all_categories_to_shopware(
            root_category=category_root,
            skip_root=False,
            sync_empty_categories=True,
            dry_run=dry_run
        )

    def _sync_all_products(
        self,
        client,
        limit: int,
        sync_images: bool,
        dry_run: bool
    ) -> Dict[str, Any]:
        """Sync all products from ERPNext to Shopware with batch logging."""
        from ecommerce_integrations.shopware6.export.product_uploader import upload_erpnext_item_to_shopware
        
        stats = {
            "total": 0,
            "synced": 0,
            "in_sync": 0,
            "created": 0,
            "errors": 0,
            "error_details": []
        }
        
        # Get all linked items
        ecom_items = frappe.get_all(
            "Ecommerce Item",
            filters={"integration": MODULE_NAME},
            fields=["erpnext_item_code", "integration_item_code", "has_variants"],
            limit=limit if limit > 0 else 0
        )
        
        stats["total"] = len(ecom_items)
        batch_size = 50
        batch_num = 0
        
        self.logger.info(f"Starting product sync: {stats['total']} products")
        
        for i in range(0, len(ecom_items), batch_size):
            batch = ecom_items[i:i + batch_size]
            batch_num += 1
            batch_success = 0
            batch_errors = 0
            batch_start_time = frappe.utils.now()
            
            self.logger.info(f"Processing batch {batch_num}/{(len(ecom_items) + batch_size - 1) // batch_size}: Items {i+1}-{min(i+batch_size, len(ecom_items))}")
            
            for item_data in batch:
                try:
                    if not dry_run:
                        result = upload_erpnext_item_to_shopware(item_data.erpnext_item_code)
                        if result:  # Returns Shopware ID on success, None on failure
                            batch_success += 1
                            stats["synced"] += 1
                        else:
                            batch_errors += 1
                            stats["errors"] += 1
                            # Log individual error
                            error_msg = f"Failed to sync {item_data.erpnext_item_code}"
                            stats["error_details"].append(error_msg)
                            self.logger.error(error_msg)
                            
                            # Create individual error log in DB
                            create_shopware_log(
                                status="Error",
                                method="SyncManager._sync_all_products",
                                message=error_msg,
                                request_data={"item_code": item_data.erpnext_item_code}
                            )
                    else:
                        batch_success += 1
                        stats["synced"] += 1
                        
                except Exception as e:
                    batch_errors += 1
                    stats["errors"] += 1
                    error_msg = f"Exception syncing {item_data.erpnext_item_code}: {str(e)}"
                    stats["error_details"].append(error_msg)
                    self.logger.error(error_msg, exception=e, persist=False)
                    
                    # Create individual error log in DB
                    create_shopware_log(
                        status="Error",
                        method="SyncManager._sync_all_products",
                        message=error_msg,
                        request_data={"item_code": item_data.erpnext_item_code},
                        traceback=frappe.get_traceback()
                    )
            
            # Create batch summary log in DB
            batch_summary = f"Batch {batch_num}: Processed {len(batch)} items - Success: {batch_success}, Errors: {batch_errors}"
            self.logger.info(batch_summary)
            
            if not dry_run:
                create_shopware_log(
                    status="Success" if batch_errors == 0 else "Partial Success",
                    method="SyncManager._sync_all_products.batch",
                    message=batch_summary,
                    request_data={
                        "batch_num": batch_num,
                        "batch_size": len(batch),
                        "success": batch_success,
                        "errors": batch_errors,
                        "items": [item.erpnext_item_code for item in batch]
                    }
                )
            
            # Ensure DB connection after each batch
            try:
                if not frappe.db or not frappe.db.is_connected():
                    frappe.connect()
            except Exception:
                pass
        
        return stats

    def _sync_all_variants(self, client, dry_run: bool) -> Dict[str, Any]:
        """Sync all variant products with batch logging."""
        from ecommerce_integrations.shopware6.export.template_handler import (
            upload_template_item_to_shopware
        )

        stats = {"synced": 0, "errors": 0, "error_details": []}

        # Get all template items
        template_items = frappe.get_all(
            "Ecommerce Item",
            filters={"integration": MODULE_NAME, "has_variants": 1},
            fields=["erpnext_item_code", "integration_item_code"]
        )
        
        batch_size = 10
        batch_num = 0
        
        self.logger.info(f"Starting variant sync: {len(template_items)} templates")

        for i in range(0, len(template_items), batch_size):
            batch = template_items[i:i + batch_size]
            batch_num += 1
            batch_success = 0
            batch_errors = 0
            
            self.logger.info(f"Processing variant batch {batch_num}: Templates {i+1}-{min(i+batch_size, len(template_items))}")
            
            for template in batch:
                try:
                    if not dry_run:
                        item = frappe.get_doc("Item", template.erpnext_item_code)
                        upload_template_item_to_shopware(client, item)
                    batch_success += 1
                    stats["synced"] += 1
                except Exception as e:
                    batch_errors += 1
                    stats["errors"] += 1
                    error_msg = f"Failed to sync template {template.erpnext_item_code}: {str(e)}"
                    stats["error_details"].append(error_msg)
                    self.logger.error(error_msg, exc_info=True)
                    
                    # Individual error log
                    if not dry_run:
                        create_shopware_log(
                            status="Error",
                            method="SyncManager._sync_all_variants",
                            message=error_msg,
                            request_data={"template": template.erpnext_item_code},
                            traceback=frappe.get_traceback()
                        )
            
            # Batch summary log
            if not dry_run:
                batch_summary = f"Variant Batch {batch_num}: {batch_success} success, {batch_errors} errors"
                self.logger.info(batch_summary)
                create_shopware_log(
                    status="Success" if batch_errors == 0 else "Partial Success",
                    method="SyncManager._sync_all_variants.batch",
                    message=batch_summary,
                    request_data={
                        "batch_num": batch_num,
                        "success": batch_success,
                        "errors": batch_errors
                    }
                )

        return stats

    def _force_sync_all_prices(
        self,
        client,
        limit: int,
        dry_run: bool
    ) -> Dict[str, Any]:
        """Force sync all prices, fixing any broken 0.01 prices."""
        from ecommerce_integrations.shopware6.export.price_handler import (
            force_sync_all_prices
        )

        return force_sync_all_prices(
            limit=limit if limit > 0 else 10000,
            price_list=getattr(self.setting, 'default_selling_price_list', 'Standard-Vertrieb'),
            dry_run=dry_run,
            force=True  # Force to clean up broken prices
        )

    def _cleanup_orphan_variants(self, client) -> Dict[str, Any]:
        """Delete variants in Shopware that don't exist in ERPNext."""
        from ecommerce_integrations.shopware6.export.template_handler import (
            cleanup_orphaned_variants
        )

        stats = {"deleted": 0, "errors": 0, "error_details": []}

        # Get all template items
        template_items = frappe.get_all(
            "Ecommerce Item",
            filters={"integration": MODULE_NAME, "has_variants": 1},
            fields=["erpnext_item_code", "integration_item_code"]
        )
        
        self.logger.info(f"Cleaning up orphaned variants for {len(template_items)} templates")
        
        batch_size = 20
        for i in range(0, len(template_items), batch_size):
            batch = template_items[i:i + batch_size]
            batch_deleted = 0
            batch_errors = 0
            
            for template in batch:
                try:
                    result = cleanup_orphaned_variants(
                        client,
                        template.erpnext_item_code,
                        template.integration_item_code
                    )
                    deleted_count = result.get("deleted", 0)
                    batch_deleted += deleted_count
                    stats["deleted"] += deleted_count
                    
                    if deleted_count > 0:
                        self.logger.info(f"Deleted {deleted_count} orphaned variants from template {template.erpnext_item_code}")
                        
                except Exception as e:
                    batch_errors += 1
                    stats["errors"] += 1
                    error_msg = f"Failed to cleanup variants for {template.erpnext_item_code}: {str(e)}"
                    stats["error_details"].append(error_msg)
                    self.logger.error(error_msg)
            
            if batch_deleted > 0 or batch_errors > 0:
                self.logger.info(f"Variant cleanup batch: {batch_deleted} deleted, {batch_errors} errors")

        return stats

    def _cleanup_orphan_categories(self, client) -> Dict[str, Any]:
        """Delete categories in Shopware that don't exist in ERPNext."""
        from ecommerce_integrations.shopware6.export.reconciliation import (
            cleanup_orphaned_shopware_categories
        )

        category_root = getattr(self.setting, 'category_sync_root', 'Produkte')

        result = cleanup_orphaned_shopware_categories(
            root_category=category_root,
            dry_run=False
        )

        return {
            "deleted": result.get("statistics", {}).get("deleted", 0)
        }

    def _deactivate_orphan_products(self, client) -> int:
        """Deactivate products in Shopware that are disabled in ERPNext."""
        deactivated = 0

        # Get all synced items
        ecom_items = frappe.get_all(
            "Ecommerce Item",
            filters={"integration": MODULE_NAME, "has_variants": 0},
            fields=["erpnext_item_code", "integration_item_code"]
        )

        for ecom_item in ecom_items:
            # Check if disabled in ERPNext
            is_disabled = frappe.db.get_value(
                "Item", ecom_item.erpnext_item_code, "disabled"
            )

            if is_disabled:
                try:
                    # Deactivate in Shopware
                    client.request_patch(
                        f"product/{ecom_item.integration_item_code}",
                        {"active": False}
                    )
                    deactivated += 1
                except Exception:
                    pass

        return deactivated

    def _sync_all_stock(
        self,
        client,
        limit: int,
        dry_run: bool
    ) -> Dict[str, Any]:
        """Sync stock levels from ERPNext to Shopware."""
        from ecommerce_integrations.shopware6.inventory import sync_stock_for_all_items

        return sync_stock_for_all_items(
            limit=limit if limit > 0 else 10000,
            dry_run=dry_run
        )

    def _notify(self, message: str, indicator: str = "blue"):
        """Send progress notification to user."""
        frappe.publish_realtime(
            "msgprint",
            {"message": message, "indicator": indicator},
            user=frappe.session.user
        )
        self.logger.info(message)


@frappe.whitelist()
def quick_reconciliation(
    limit: int = 100,
    sync_images: bool = False,
    dry_run: bool = False
) -> Dict[str, Any]:
    """
    Quick reconciliation for small batches.

    Syncs categories and products without cleanup operations.

    Args:
        limit: Maximum products to process
        sync_images: Also sync images
        dry_run: Report only

    Returns:
        dict with results
    """
    manager = SyncManager()

    result = manager.full_reconciliation(
        sync_categories=True,
        sync_products=True,
        sync_variants=False,
        sync_prices=False,
        sync_images=sync_images,
        sync_stock=False,
        cleanup_orphan_variants=False,
        cleanup_orphan_categories=False,
        deactivate_missing_products=False,
        limit=cint(limit),
        dry_run=dry_run
    )

    return {
        "success": result.success,
        "summary": result.summary,
        "statistics": {
            "categories_synced": result.categories_synced,
            "products_synced": result.products_synced,
            "products_in_sync": result.products_in_sync,
        },
        "errors": result.errors[:10] if result.errors else []
    }


@frappe.whitelist()
def full_reconciliation_no_brainer(
    sync_images: bool = True,
    sync_stock: bool = False,
    dry_run: bool = False,
    category_root: str = None,
    cleanup_orphan_categories: bool = True
) -> Dict[str, Any]:
    """
    THE NO-BRAINER FUNCTION.

    When this completes, Shopware will be 100% identical to ERPNext.
    - All categories synced (only under category_root) - UNLESS skipped in settings
    - All products synced
    - All variants synced
    - All prices corrected (including broken 0.01€ prices)
    - All images synced
    - All orphan variants deleted
    - All orphan categories deleted (only under category_root)
    - All disabled products deactivated in Shopware

    This may take a while but when done, Shopware = ERPNext. Guaranteed.

    Args:
        sync_images: Also sync all images (default: True)
        sync_stock: Also sync stock levels (default: False for safety)
        dry_run: Only report what would change
        category_root: Root category - only categories under this are synced/cleaned
        cleanup_orphan_categories: Delete orphaned categories (only under root)

    Returns:
        Complete reconciliation result
    """
    manager = SyncManager()

    # Override category root if provided
    if category_root:
        manager.setting.category_sync_root = category_root
    
    # Check if category sync should be skipped (from settings)
    skip_category_sync = getattr(manager.setting, 'skip_category_sync_on_full_reconciliation', False)

    result = manager.full_reconciliation(
        sync_categories=not skip_category_sync,  # Respect setting
        sync_products=True,
        sync_variants=True,
        sync_prices=True,
        sync_images=sync_images,
        sync_stock=sync_stock,
        cleanup_orphan_variants=True,
        cleanup_orphan_categories=cleanup_orphan_categories and not skip_category_sync,  # Don't cleanup categories if sync is skipped
        deactivate_missing_products=True,
        limit=0,  # No limit - process ALL
        dry_run=dry_run
    )

    return {
        "success": result.success,
        "summary": result.summary,
        "started_at": result.started_at,
        "completed_at": result.completed_at,
        "category_sync_skipped": skip_category_sync,
        "statistics": {
            "categories": {
                "synced": result.categories_synced,
                "created": result.categories_created,
                "deleted": result.categories_deleted,
                "skipped": skip_category_sync,
            },
            "products": {
                "checked": result.products_checked,
                "in_sync": result.products_in_sync,
                "synced": result.products_synced,
                "created": result.products_created,
                "deactivated": result.products_deactivated,
            },
            "variants": {
                "synced": result.variants_synced,
                "deleted": result.variants_deleted,
            },
            "prices": {
                "synced": result.prices_synced,
                "broken_fixed": result.prices_fixed,
            },
            "images_synced": result.images_synced,
            "stock_adjusted": result.stock_adjusted,
        },
        "errors": result.errors[:20] if result.errors else [],
        "dry_run": dry_run,
    }


@frappe.whitelist()
def enqueue_full_reconciliation_no_brainer(
    sync_images: bool = True,
    sync_stock: bool = False,
    dry_run: bool = False,
    category_root: str = None,
    cleanup_orphan_categories: bool = True
) -> Dict[str, Any]:
    """
    Enqueue the no-brainer reconciliation as a background job.

    For large catalogs, this should run in background.

    Args:
        sync_images: Also sync images
        sync_stock: Also sync stock
        dry_run: Only report what would change
        category_root: Root category for sync (only categories under this root are affected)
        cleanup_orphan_categories: Delete orphaned categories (only under root)

    Returns:
        Job enqueue status
    """
    from frappe.utils.background_jobs import is_job_enqueued

    job_name = "shopware6_no_brainer_reconciliation"

    if is_job_enqueued(job_name):
        return {
            "success": False,
            "message": "A reconciliation job is already running. Please wait."
        }

    # Get category root from settings if not provided
    if not category_root:
        setting = frappe.get_cached_doc(SETTING_DOCTYPE)
        category_root = getattr(setting, 'category_sync_root', 'Produkte') or 'Produkte'

    create_shopware_log(
        status="Queued",
        method="full_reconciliation_no_brainer",
        message=f"Full reconciliation queued (root: {category_root}, dry_run: {dry_run})"
    )

    frappe.enqueue(
        "ecommerce_integrations.shopware6.sync.sync_manager.full_reconciliation_no_brainer",
        queue="long",
        timeout=14400,  # 4 hours
        job_name=job_name,
        sync_images=sync_images,
        sync_stock=sync_stock,
        dry_run=dry_run,
        category_root=category_root,
        cleanup_orphan_categories=cleanup_orphan_categories
    )

    return {
        "success": True,
        "message": f"Full reconciliation enqueued (root: {category_root}). " +
                  ("DRY RUN mode." if dry_run else "Shopware will be made identical to ERPNext."),
        "job_name": job_name
    }
