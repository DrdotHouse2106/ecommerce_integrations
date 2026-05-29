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

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import frappe
from frappe import _
from frappe.utils import cint, now

# CRITICAL: ShopwareAPIError inherits from BaseException, NOT Exception!
# We must catch it explicitly alongside Exception in all error handlers.
from lib_shopware6_api_base.conf_shopware6_api_base_classes import ShopwareAPIError

from ecommerce_integrations.shopware6.connection import get_shopware_client
from ecommerce_integrations.shopware6.constants import MODULE_NAME, SETTING_DOCTYPE
from ecommerce_integrations.shopware6.utils import create_shopware_log, get_logger, update_shopware_log

# Generation counter — newer reconciliation runs supersede in-flight older ones.
SHOPWARE_RECONCILE_GENERATION_KEY = "shopware6_reconcile_generation"


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

    # Property cleanup stats
    properties_deleted: int = 0

    # Error tracking
    errors: list[dict] = field(default_factory=list)

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

        if self.variants_deleted or self.products_deactivated or self.properties_deleted:
            parts.append(
                f"Cleaned: {self.variants_deleted} orphan variants, "
                f"{self.properties_deleted} orphan properties, "
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

    def __init__(self, log_name: str | None = None):
        self.setting = frappe.get_cached_doc(SETTING_DOCTYPE)
        self.logger = get_logger("SyncManager")
        self._log_name = log_name

        if not self.setting.is_enabled():
            frappe.throw(_("Shopware integration is not enabled"))

    def sync_item(self, item_code: str, sync_images: bool = True) -> dict[str, Any]:
        """
        Sync a single item to Shopware.

        Args:
            item_code: ERPNext Item code
            sync_images: Also sync images

        Returns:
            dict with sync result
        """
        from ecommerce_integrations.shopware6.export.product_uploader import sync_item_if_changed

        result = sync_item_if_changed(item_code, force=False)

        if result.get("synced"):
            self.logger.info(f"Synced {item_code}: {result.get('message')}")

        return result

    def sync_items_batch(
        self,
        item_codes: list[str],
        sync_images: bool = True
    ) -> dict[str, Any]:
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
        cleanup_orphan_properties: bool = True,
        deactivate_missing_products: bool = True,
        limit: int = 0,  # 0 = no limit
        dry_run: bool = False,
        use_batch_sync: bool = True,  # Use Batch API for 5-10x speedup
        sync_generation: int | None = None,
    ) -> ReconciliationResult:
        """
        Full reconciliation - THE NO-BRAINER function.

        Each phase is wrapped by ``_run_phase`` so the try / commit /
        ``_safe_db_recover`` / error-append pattern is declared once. A
        ``sync_generation`` value can be passed by the enqueue wrapper; if a
        newer generation appears between phases the run aborts cleanly.

        Returns:
            ReconciliationResult with complete statistics
        """
        client = get_shopware_client()
        result = ReconciliationResult(started_at=now())

        self.logger.info(
            f"Starting full reconciliation (dry_run={dry_run}, limit={limit}, generation={sync_generation})"
        )

        self._sync_generation = sync_generation
        self._aborted_by_newer_generation = False

        try:
            # Phase 1: Categories
            if sync_categories:
                self._run_phase(
                    "1_categories",
                    "Phase 1/6: Syncing categories...",
                    lambda: self._sync_all_categories(client, dry_run),
                    lambda r: (
                        setattr(result, "categories_synced", r.get("synced", 0)),
                        setattr(result, "categories_created", r.get("created", 0)),
                    ),
                    result,
                )
            else:
                self._notify("Phase 1/6: Skipping categories (disabled in settings)...")

            if self._aborted_by_newer_generation:
                return result

            # Phase 2 & 3: Products and Variants
            if sync_products or sync_variants:
                self._run_phase(
                    "2_3_products",
                    "Phase 2-3/6: Syncing products & variants...",
                    lambda: self._run_products_variants_phase(
                        client, sync_products, sync_variants, sync_images,
                        limit, dry_run, use_batch_sync,
                    ),
                    lambda r: self._apply_products_variants_result(result, r),
                    result,
                )

            if self._aborted_by_newer_generation:
                return result

            # Phase 4: Force Price Sync
            if sync_prices:
                msg = (
                    "Phase 4/6: Batch-syncing prices (Sync API)..."
                    if use_batch_sync and not dry_run
                    else "Phase 4/6: Syncing prices..."
                )
                self._run_phase(
                    "4_prices",
                    msg,
                    lambda: self._force_sync_all_prices(client, limit, dry_run, use_batch=use_batch_sync),
                    lambda r: (
                        setattr(result, "prices_synced", r.get("synced", 0)),
                        setattr(result, "prices_fixed", r.get("broken_fixed", 0)),
                    ),
                    result,
                )

            if self._aborted_by_newer_generation:
                return result

            # Phase 5: Cleanup orphans
            msg = (
                "Phase 5/6: Batch-cleaning orphans (Sync API)..."
                if use_batch_sync and not dry_run
                else "Phase 5/6: Cleaning up orphans..."
            )
            self._run_phase(
                "5_cleanup",
                msg,
                lambda: self._run_cleanup_phase(
                    client, cleanup_orphan_variants, cleanup_orphan_categories,
                    cleanup_orphan_properties, deactivate_missing_products,
                    dry_run, use_batch_sync,
                ),
                lambda r: self._apply_cleanup_result(result, r),
                result,
            )

            if self._aborted_by_newer_generation:
                return result

            # Phase 6: Stock sync (if enabled)
            if sync_stock:
                self._run_phase(
                    "6_stock",
                    "Phase 6/6: Syncing stock levels...",
                    lambda: self._sync_all_stock(client, limit, dry_run),
                    lambda r: setattr(result, "stock_adjusted", r.get("adjusted", 0)),
                    result,
                )
            else:
                self._notify("Phase 6/6: Stock sync skipped (disabled)")

            result.completed_at = now()
            result.success = True

            create_shopware_log(
                status="Success",
                method="SyncManager.full_reconciliation",
                message=result.summary,
            )

        except (Exception, ShopwareAPIError) as e:
            result.success = False
            result.errors.append({"phase": "general", "error": str(e)})
            self.logger.error("Full reconciliation failed", exception=e)

        return result

    def _run_phase(
        self,
        name: str,
        notify_message: str,
        fn: Callable[[], dict[str, Any]],
        on_result: Callable[[dict[str, Any]], Any],
        result: "ReconciliationResult",
    ) -> None:
        """Wrap one reconciliation phase: notify, run, commit, recover on error.

        Also checks the generation counter before each phase — if a newer
        reconciliation has been enqueued since this run started, mark the run
        as aborted and return without executing the phase body.
        """
        if self._is_superseded():
            self.logger.info(
                f"Phase {name} skipped: superseded by newer reconciliation "
                f"(self_generation={self._sync_generation})"
            )
            self._aborted_by_newer_generation = True
            return

        self._notify(notify_message)
        self.logger.info(f"Full Reconciliation: Starting Phase {name}")

        try:
            phase_result = fn() or {}
            on_result(phase_result)
            frappe.db.commit()
            self.logger.info(f"Full Reconciliation: Phase {name} complete")
        except (Exception, ShopwareAPIError) as e:
            self._safe_db_recover()
            result.errors.append({"phase": name, "error": str(e)[:200]})
            self.logger.error(f"Phase {name} failed: {e}")

    def _is_superseded(self) -> bool:
        """Return True if a newer reconciliation run has been enqueued."""
        gen = getattr(self, "_sync_generation", None)
        if not gen:
            return False
        try:
            current = frappe.cache.get_value(SHOPWARE_RECONCILE_GENERATION_KEY)
        except Exception:
            return False
        try:
            return current is not None and int(current) > int(gen)
        except (TypeError, ValueError):
            return False

    def _run_products_variants_phase(
        self,
        client,
        sync_products: bool,
        sync_variants: bool,
        sync_images: bool,
        limit: int,
        dry_run: bool,
        use_batch_sync: bool,
    ) -> dict[str, Any]:
        """Phase 2+3 body: products and variants, batch or sequential."""
        if use_batch_sync and not dry_run:
            batch_result = self._sync_all_products_batch(client, sync_images)
            checked = batch_result["templates"]["total"] + batch_result["simple"]["total"]
            synced = batch_result["templates"]["success"] + batch_result["simple"]["success"]
            return {
                "mode": "batch",
                "products_checked": checked,
                "products_synced": synced,
                "products_created": synced,
                "variants_synced": batch_result["variants"]["success"],
                "errors": batch_result.get("errors", []),
            }

        out: dict[str, Any] = {"mode": "sequential", "errors": []}
        if sync_products:
            prod = self._sync_all_products(client, limit, sync_images, dry_run)
            out["products_checked"] = prod.get("total", 0)
            out["products_synced"] = prod.get("synced", 0)
            out["products_in_sync"] = prod.get("in_sync", 0)
            out["products_created"] = prod.get("created", 0)
            out["errors"].extend(prod.get("errors", []))
        if sync_variants:
            var = self._sync_all_variants(client, dry_run)
            out["variants_synced"] = var.get("synced", 0)
        return out

    def _apply_products_variants_result(
        self, result: "ReconciliationResult", r: dict[str, Any]
    ) -> None:
        for key in (
            "products_checked", "products_synced", "products_in_sync",
            "products_created", "variants_synced",
        ):
            if key in r:
                setattr(result, key, r[key])
        if r.get("errors"):
            result.errors.extend(r["errors"])

    def _run_cleanup_phase(
        self,
        client,
        cleanup_orphan_variants: bool,
        cleanup_orphan_categories: bool,
        cleanup_orphan_properties: bool,
        deactivate_missing_products: bool,
        dry_run: bool,
        use_batch_sync: bool,
    ) -> dict[str, Any]:
        """Phase 5 body: delete orphans + deactivate missing products."""
        out: dict[str, Any] = {}
        if cleanup_orphan_variants and not dry_run:
            out["variants_deleted"] = self._cleanup_orphan_variants(
                client, use_batch=use_batch_sync
            ).get("deleted", 0)
        if cleanup_orphan_categories and not dry_run:
            out["categories_deleted"] = self._cleanup_orphan_categories(client).get("deleted", 0)
        if cleanup_orphan_properties and not dry_run:
            out["properties_deleted"] = self._cleanup_orphan_properties(
                client, use_batch=use_batch_sync
            ).get("groups_deleted", 0)
        if deactivate_missing_products and not dry_run:
            out["products_deactivated"] = self._deactivate_orphan_products(client)
        return out

    def _apply_cleanup_result(
        self, result: "ReconciliationResult", r: dict[str, Any]
    ) -> None:
        for key in (
            "variants_deleted", "categories_deleted",
            "properties_deleted", "products_deactivated",
        ):
            if key in r:
                setattr(result, key, r[key])

    def _sync_all_categories(self, client, dry_run: bool) -> dict[str, Any]:
        """Sync all categories from ERPNext to Shopware."""
        from ecommerce_integrations.shopware6.export.reconciliation import sync_all_categories_to_shopware

        category_root = getattr(self.setting, 'category_sync_root', 'Products') or 'Products'

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
    ) -> dict[str, Any]:
        """Sync all products from ERPNext to Shopware with batch logging."""
        from ecommerce_integrations.product_sync.compat import (
            push_item_via_engine as upload_erpnext_item_to_shopware,
        )

        stats = {
            "total": 0,
            "synced": 0,
            "in_sync": 0,
            "created": 0,
            "errors": [],  # List of error dicts for detailed reporting
            "error_count": 0
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
                            stats["error_count"] += 1
                            # Log individual error with details
                            error_entry = {
                                "item": item_data.erpnext_item_code,
                                "error": "Upload returned None - check product data",
                                "phase": "product_upload"
                            }
                            stats["errors"].append(error_entry)
                            self.logger.error(f"Failed to sync {item_data.erpnext_item_code}: Upload returned None")

                            # Create individual error log in DB
                            create_shopware_log(
                                status="Error",
                                method="SyncManager._sync_all_products",
                                message=f"Failed to sync {item_data.erpnext_item_code}",
                                request_data=error_entry
                            )
                    else:
                        batch_success += 1
                        stats["synced"] += 1

                except Exception as e:
                    batch_errors += 1
                    stats["error_count"] += 1
                    tb = frappe.get_traceback()
                    error_entry = {
                        "item": item_data.erpnext_item_code,
                        "error": str(e),
                        "phase": "product_upload",
                        "traceback": tb[:1500] if tb else None  # Include traceback for debugging
                    }
                    stats["errors"].append(error_entry)
                    self.logger.error(f"Exception syncing {item_data.erpnext_item_code}: {e!s}", exception=e)

                    # Create individual error log in DB
                    create_shopware_log(
                        status="Error",
                        method="SyncManager._sync_all_products",
                        message=f"Exception syncing {item_data.erpnext_item_code}: {e!s}",
                        request_data=error_entry,
                        traceback=tb
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
                frappe.db.sql("SELECT 1")
            except Exception:
                try:
                    frappe.connect()
                except Exception:
                    pass

        return stats

    def _sync_all_variants(self, client, dry_run: bool) -> dict[str, Any]:
        """Sync all variant products with batch logging."""
        from ecommerce_integrations.shopware6.export.template_handler import upload_template_item_to_shopware

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
                    # Get full traceback for better debugging
                    tb = frappe.get_traceback()
                    error_msg = f"Failed to sync template {template.erpnext_item_code}: {e!s}"
                    stats["error_details"].append({
                        "item": template.erpnext_item_code,
                        "error": str(e),
                        "traceback": tb[:1000] if tb else None  # Limit traceback length
                    })
                    self.logger.error(error_msg, exception=e)

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

    def _sync_all_products_batch(self, client, sync_images: bool = True) -> dict[str, Any]:
        """
        Batch-sync all products using Shopware Sync API (5-10x faster).

        Uses BatchProductUploader for bulk operations instead of individual HTTP calls.
        Templates are synced first (parents must exist before variants).

        Args:
            client: Shopware API client
            sync_images: Also sync product images

        Returns:
            Dict with batch sync results for templates, simple items, and variants
        """
        from ecommerce_integrations.shopware6.export.batch_uploader import (
            BatchProductUploader,
            get_all_item_codes_by_type,
        )
        from ecommerce_integrations.shopware6.export.image_handler import sync_product_images_to_shopware
        from ecommerce_integrations.shopware6.utils import get_shopware_document_id

        self.logger.info("Using Batch Sync for products (Sync API - 5-10x faster)")

        items_by_type = get_all_item_codes_by_type()

        self.logger.info(
            f"Found: {len(items_by_type['templates'])} templates, "
            f"{len(items_by_type['simple_items'])} simple, "
            f"{len(items_by_type['variants'])} variants"
        )

        def progress_callback(batch_num, total_batches, processed, total, success, failed):
            self._notify(f"Batch {batch_num}/{total_batches}: {processed}/{total} ({success} ok, {failed} err)")
            self.logger.info(f"Batch progress: {batch_num}/{total_batches} - {processed}/{total}")

        uploader = BatchProductUploader(progress_callback=progress_callback)

        # Phase 2a: Templates first (Parent products must exist before variants)
        # Check if templates should be skipped (set by full_reconciliation_no_brainer)
        skip_templates = getattr(self, '_skip_templates', False)

        if skip_templates:
            self._notify("Phase 2a/6: Templates übersprungen (skip_templates=True)")
            self.logger.info("Batch Sync: Templates SKIPPED per user request")
            from ecommerce_integrations.shopware6.export.batch_uploader import BatchUploadResult
            templates_result = BatchUploadResult(total=len(items_by_type['templates']))
            templates_result.skipped = len(items_by_type['templates'])
        else:
            self._notify("Phase 2a/6: Batch-syncing templates...")
            self.logger.info("Batch Sync: Starting templates")
            templates_result = uploader.upload_templates(
                items_by_type['templates'],
                skip_images=not sync_images
            )
            self.logger.info(
                f"Batch Sync: Templates complete - "
                f"{templates_result.success}/{templates_result.total} success"
            )

        # Phase 2b: Simple Items
        self._notify("Phase 2b/6: Batch-syncing simple items...")
        self.logger.info("Batch Sync: Starting simple items")
        simple_result = uploader.upload_items(
            items_by_type['simple_items'],
            skip_images=not sync_images
        )
        self.logger.info(
            f"Batch Sync: Simple items complete - "
            f"{simple_result.success}/{simple_result.total} success, {simple_result.skipped} skipped"
        )

        # Phase 3: Variants last (require parent products)
        self._notify("Phase 3/6: Batch-syncing variants...")
        self.logger.info("Batch Sync: Starting variants")
        variants_result = uploader.upload_variants(
            items_by_type['variants'],
            skip_images=not sync_images
        )
        self.logger.info(
            f"Batch Sync: Variants complete - "
            f"{variants_result.success}/{variants_result.total} success, {variants_result.skipped} skipped"
        )

        # Aggregate all errors
        all_errors = []
        all_errors.extend(templates_result.errors)
        all_errors.extend(simple_result.errors)
        all_errors.extend(variants_result.errors)

        # Image Sync Phase (after all products are uploaded)
        images_synced = 0
        if sync_images:
            all_processed = (
                templates_result.processed_item_codes +
                simple_result.processed_item_codes +
                variants_result.processed_item_codes
            )
            if all_processed:
                self._notify(f"Phase 3b/6: Syncing images for {len(all_processed)} products...")
                self.logger.info(f"Image Sync: Starting for {len(all_processed)} products")

                for i, item_code in enumerate(all_processed):
                    try:
                        shopware_id = get_shopware_document_id("Item", item_code)
                        if shopware_id:
                            item = frappe.get_doc("Item", item_code)
                            if sync_product_images_to_shopware(client, item, shopware_id):
                                images_synced += 1
                    except Exception as e:
                        self.logger.warning(f"Image sync failed for {item_code}: {e}")

                    # Progress notification every 50 items
                    if (i + 1) % 50 == 0:
                        self._notify(f"Images: {i + 1}/{len(all_processed)} ({images_synced} synced)")

                self.logger.info(f"Image Sync: Complete - {images_synced} images synced")

        return {
            'templates': {
                'total': templates_result.total,
                'success': templates_result.success,
                'failed': templates_result.failed,
            },
            'simple': {
                'total': simple_result.total,
                'success': simple_result.success,
                'failed': simple_result.failed,
                'skipped': simple_result.skipped,
            },
            'variants': {
                'total': variants_result.total,
                'success': variants_result.success,
                'failed': variants_result.failed,
                'skipped': variants_result.skipped,
            },
            'images_synced': images_synced,
            'errors': all_errors
        }

    def _force_sync_all_prices(
        self,
        client,
        limit: int,
        dry_run: bool,
        use_batch: bool = True
    ) -> dict[str, Any]:
        """Force sync all prices, fixing any broken 0.01 prices."""
        price_list = getattr(self.setting, 'default_selling_price_list', 'Standard-Vertrieb')

        if use_batch and not dry_run:
            # BATCH MODE: Use Sync API for 5-10x speedup
            from ecommerce_integrations.shopware6.export.price_handler import (
                delete_broken_price_rules_batch,
                force_sync_prices_batch,
            )

            self.logger.info("Using Batch Sync for prices (Sync API - 5-10x faster)")

            # First delete broken price rules
            delete_result = delete_broken_price_rules_batch(client)
            broken_fixed = delete_result.get("statistics", {}).get("deleted", 0)

            # Then batch-sync all prices
            def progress_callback(batch_num, total_batches, processed, total):
                self._notify(f"Price batch {batch_num}/{total_batches}: {processed}/{total}")

            result = force_sync_prices_batch(
                price_list=price_list,
                progress_callback=progress_callback
            )

            stats = result.get("statistics", {})
            return {
                "synced": stats.get("updated", 0),
                "broken_fixed": broken_fixed,
                "deactivated": stats.get("deactivated", 0),
                "errors": stats.get("errors", [])
            }
        else:
            # SEQUENTIAL MODE: Original behavior (for dry_run or explicit disable)
            from ecommerce_integrations.shopware6.export.price_handler import force_sync_all_prices

            return force_sync_all_prices(
                limit=limit if limit > 0 else 10000,
                price_list=price_list,
                dry_run=dry_run,
                force=True
            )

    def _cleanup_orphan_variants(self, client, use_batch: bool = True) -> dict[str, Any]:
        """Delete variants in Shopware that don't exist in ERPNext."""
        if use_batch:
            # BATCH MODE: Use Sync API for faster deletion
            from ecommerce_integrations.shopware6.export.template_handler import (
                cleanup_all_orphaned_variants_batch,
            )

            self.logger.info("Using Batch Sync for orphan variant cleanup")

            def progress_callback(template_num, total, deleted_count):
                self._notify(f"Variant cleanup: {template_num}/{total} templates, {deleted_count} orphans found")

            result = cleanup_all_orphaned_variants_batch(client, progress_callback)
            stats = result.get("statistics", {})

            return {
                "deleted": stats.get("variants_deleted", 0),
                "errors": len(stats.get("errors", [])),
                "error_details": stats.get("errors", [])
            }
        else:
            # SEQUENTIAL MODE: Original behavior
            from ecommerce_integrations.shopware6.export.template_handler import cleanup_orphaned_variants

            stats = {"deleted": 0, "errors": 0, "error_details": []}

            template_items = frappe.get_all(
                "Ecommerce Item",
                filters={"integration": MODULE_NAME, "has_variants": 1},
                fields=["erpnext_item_code", "integration_item_code"]
            )

            self.logger.info(f"Cleaning up orphaned variants for {len(template_items)} templates")

            for template in template_items:
                try:
                    result = cleanup_orphaned_variants(
                        client,
                        template.erpnext_item_code,
                        template.integration_item_code
                    )
                    stats["deleted"] += result.get("deleted", 0)
                except Exception as e:
                    stats["errors"] += 1
                    stats["error_details"].append(str(e)[:100])

            return stats

    def _cleanup_orphan_categories(self, client) -> dict[str, Any]:
        """Delete categories in Shopware that don't exist in ERPNext."""
        from ecommerce_integrations.shopware6.export.reconciliation import (
            cleanup_orphaned_shopware_categories,
        )

        category_root = getattr(self.setting, 'category_sync_root', 'Products')

        result = cleanup_orphaned_shopware_categories(
            root_category=category_root,
            dry_run=False
        )

        return {
            "deleted": result.get("statistics", {}).get("deleted", 0)
        }

    def _cleanup_orphan_properties(self, client, use_batch: bool = True) -> dict[str, Any]:
        """Delete property groups in Shopware that don't exist in ERPNext."""
        if use_batch:
            # BATCH MODE: Use Sync API for faster deletion
            from ecommerce_integrations.shopware6.export.property_handler import (
                cleanup_orphaned_properties_batch,
            )

            self.logger.info("Using Batch Sync for orphan property cleanup")
            result = cleanup_orphaned_properties_batch(client)
            stats = result.get("statistics", {})

            if stats.get("groups_deleted", 0) > 0:
                self.logger.info(
                    f"Batch deleted {stats['groups_deleted']} orphaned property groups, "
                    f"{stats.get('options_deleted', 0)} options"
                )

            return {
                "groups_deleted": stats.get("groups_deleted", 0),
                "options_deleted": stats.get("options_deleted", 0),
                "errors": stats.get("errors", [])
            }
        else:
            # SEQUENTIAL MODE: Original behavior
            from ecommerce_integrations.shopware6.export.property_handler import (
                cleanup_orphaned_shopware_properties,
            )

            result = cleanup_orphaned_shopware_properties(client, dry_run=False)

            if result.get("groups_deleted", 0) > 0:
                self.logger.info(f"Deleted {result['groups_deleted']} orphaned property groups from Shopware")

            return result

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
    ) -> dict[str, Any]:
        """Sync stock levels from ERPNext to Shopware."""
        from ecommerce_integrations.shopware6.inventory import sync_stock_for_all_items

        return sync_stock_for_all_items(
            limit=limit if limit > 0 else 10000,
            dry_run=dry_run
        )

    def _safe_db_recover(self):
        """Safely recover DB connection after an error — never raises."""
        try:
            frappe.db.rollback()
        except Exception:
            try:
                frappe.connect()
            except Exception:
                pass

    def _notify(self, message: str, indicator: str = "blue"):
        """Log progress message and update the integration log if available."""
        self.logger.info(message)
        # Update the Ecommerce Integration Log document with progress
        if self._log_name:
            try:
                update_shopware_log(self._log_name, message=message)
            except Exception:
                pass  # Don't let log updates break the sync


@frappe.whitelist()
def quick_reconciliation(
    limit: int = 100,
    sync_images: bool = False,
    dry_run: bool = False
) -> dict[str, Any]:
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
    frappe.only_for("System Manager")
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
    category_root: str | None = None,
    cleanup_orphan_categories: bool = True,
    # Skip options for granular control
    skip_categories: bool = False,
    skip_templates: bool = False,
    skip_products: bool = False,
    skip_variants: bool = False,
    skip_prices: bool = False,
    skip_cleanup: bool = False,
    log_name: str | None = None,
    sync_generation: int | None = None,
) -> dict[str, Any]:
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
        skip_categories: Skip category sync phase
        skip_products: Skip product sync phase
        skip_variants: Skip variant sync phase
        skip_prices: Skip price correction phase
        skip_cleanup: Skip orphan cleanup phases
        category_root: Root category - only categories under this are synced/cleaned
        cleanup_orphan_categories: Delete orphaned categories (only under root)
        log_name: Name of the Ecommerce Integration Log entry to update with progress

    Returns:
        Complete reconciliation result
    """
    frappe.only_for("System Manager")
    # Update log to "Running" immediately
    if log_name:
        try:
            update_shopware_log(log_name, status="Running", message="Full reconciliation starting...")
        except Exception:
            pass

    try:
        manager = SyncManager(log_name=log_name)

        # Override category root if provided
        if category_root:
            manager.setting.category_sync_root = category_root

        # Check if category sync should be skipped (from settings or parameter)
        skip_category_sync = skip_categories or getattr(
            manager.setting, 'skip_category_sync_on_full_reconciliation', False
        )

        # Store skip_templates flag on manager for use in _sync_all_products_batch
        manager._skip_templates = skip_templates

        result = manager.full_reconciliation(
            sync_categories=not skip_category_sync,
            sync_products=not skip_products,
            sync_variants=not skip_variants,
            sync_prices=not skip_prices,
            sync_images=sync_images,
            sync_stock=sync_stock,
            cleanup_orphan_variants=not skip_cleanup,
            cleanup_orphan_categories=cleanup_orphan_categories and not skip_category_sync and not skip_cleanup,
            cleanup_orphan_properties=not skip_cleanup,
            deactivate_missing_products=not skip_cleanup,
            limit=0,  # No limit - process ALL
            dry_run=dry_run,
            use_batch_sync=True,  # Use Batch Sync API for 5-10x speedup
            sync_generation=sync_generation,
        )

        response = {
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
                "properties": {
                    "deleted": result.properties_deleted,
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

        # Update the log entry to Success with summary
        if log_name:
            try:
                final_status = "Success" if not result.errors else "Partial Success"
                update_shopware_log(
                    log_name,
                    status=final_status,
                    message=result.summary,
                    response_data=response,
                )
            except Exception:
                pass

        return response
    except (Exception, ShopwareAPIError) as e:
        import traceback
        error_msg = f"Full reconciliation crashed: {e}\n{traceback.format_exc()}"
        get_logger().error(error_msg, persist=True)

        # Update the original log entry to Error
        if log_name:
            try:
                update_shopware_log(
                    log_name,
                    status="Error",
                    message=error_msg[:10000],
                    exception=traceback.format_exc(),
                )
            except Exception:
                pass
        else:
            # Fallback: create a separate error log if no log_name
            try:
                create_shopware_log(
                    status="Error",
                    method="full_reconciliation_no_brainer",
                    message=error_msg[:10000],
                )
            except Exception:
                pass

        raise


@frappe.whitelist()
def enqueue_full_reconciliation_no_brainer(
    sync_images: bool = True,
    sync_stock: bool = False,
    dry_run: bool = False,
    category_root: str | None = None,
    cleanup_orphan_categories: bool = True,
    # Skip options for granular control
    skip_categories: bool = False,
    skip_templates: bool = False,
    skip_products: bool = False,
    skip_variants: bool = False,
    skip_prices: bool = False,
    skip_cleanup: bool = False,
) -> dict[str, Any]:
    """
    Enqueue the no-brainer reconciliation as a background job.

    For large catalogs, this should run in background.

    Args:
        sync_images: Also sync images
        sync_stock: Also sync stock
        dry_run: Only report what would change
        category_root: Root category for sync (only categories under this root are affected)
        cleanup_orphan_categories: Delete orphaned categories (only under root)
        skip_categories: Skip category sync phase
        skip_products: Skip product sync phase
        skip_variants: Skip variant sync phase
        skip_prices: Skip price correction phase
        skip_cleanup: Skip orphan cleanup phases

    Returns:
        Job enqueue status
    """
    frappe.only_for("System Manager")

    # Generation counter supersedes the old is_job_enqueued gate:
    # bumping the counter signals every in-flight run to abort on its next
    # phase boundary while the freshly-enqueued run takes over.
    try:
        generation = frappe.cache.incr(SHOPWARE_RECONCILE_GENERATION_KEY)
    except Exception:
        # Fallback if redis isn't reachable for any reason — degrade to "no gate"
        generation = None

    job_name = f"shopware6_no_brainer_reconciliation_{generation}" if generation else "shopware6_no_brainer_reconciliation"

    # Get category root from settings if not provided
    if not category_root:
        setting = frappe.get_cached_doc(SETTING_DOCTYPE)
        category_root = getattr(setting, 'category_sync_root', 'Products') or 'Products'

    # Build skip info for log message
    skip_info = []
    if skip_categories:
        skip_info.append("categories")
    if skip_templates:
        skip_info.append("templates")
    if skip_products:
        skip_info.append("products")
    if skip_variants:
        skip_info.append("variants")
    if skip_prices:
        skip_info.append("prices")
    if skip_cleanup:
        skip_info.append("cleanup")
    if not sync_images:
        skip_info.append("images")

    skip_msg = f", skipping: {', '.join(skip_info)}" if skip_info else ""

    log = create_shopware_log(
        status="Queued",
        method="full_reconciliation_no_brainer",
        message=f"Full reconciliation queued (root: {category_root}, dry_run: {dry_run}{skip_msg})"
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
        cleanup_orphan_categories=cleanup_orphan_categories,
        skip_categories=skip_categories,
        skip_templates=skip_templates,
        skip_products=skip_products,
        skip_variants=skip_variants,
        skip_prices=skip_prices,
        skip_cleanup=skip_cleanup,
        log_name=log.name,
        sync_generation=generation,
    )

    return {
        "success": True,
        "message": f"Full reconciliation enqueued (root: {category_root}). " +
                  ("DRY RUN mode." if dry_run else "Shopware will be made identical to ERPNext.") +
                  (f" Skipping: {', '.join(skip_info)}." if skip_info else ""),
        "job_name": job_name
    }
