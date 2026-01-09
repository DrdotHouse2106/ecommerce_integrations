"""
Shopware 6 Bulk Sync Queue

Prevents ERPNext from crashing during bulk product updates by:
1. Queueing items for sync instead of syncing immediately
2. Detecting bulk updates and deferring to batch processing
3. Processing queued items in controlled batches

Usage:
    Instead of calling upload_erpnext_item_to_shopware directly,
    call queue_item_for_sync which will either:
    - Queue the item for later bulk processing (during bulk updates)
    - Process immediately (for single updates)
"""

import time
from typing import List, Optional, Set
import frappe
from frappe.utils import now_datetime, cint
from ecommerce_integrations.shopware6.constants import SETTING_DOCTYPE
from ecommerce_integrations.shopware6.utils import get_logger

# Module logger
logger = get_logger("bulk_sync")

# Redis key prefixes
SYNC_QUEUE_KEY = "shopware6:sync_queue"
SYNC_LOCK_KEY = "shopware6:sync_lock"
LAST_SYNC_REQUEST_KEY = "shopware6:last_sync_request"
BULK_MODE_KEY = "shopware6:bulk_mode"
REQUEST_COUNT_KEY = "shopware6:request_count"

# Default Configuration (can be overridden in Shopware Settings)
DEFAULT_BULK_THRESHOLD = 5  # Number of requests within BULK_WINDOW to trigger bulk mode
BULK_WINDOW = 2  # Seconds to count requests
BULK_COOLDOWN = 10  # Seconds to wait after last request before processing bulk queue
DEFAULT_BATCH_SIZE = 50  # Number of items to process per batch
BATCH_DELAY = 0.1  # Seconds to wait between batches (reduced from 1s for performance)


def get_bulk_sync_settings():
    """Get bulk sync settings from Shopware Settings doctype."""
    try:
        setting = frappe.get_cached_doc(SETTING_DOCTYPE)
        return {
            "enabled": cint(getattr(setting, "enable_bulk_sync", 1)),
            "threshold": cint(getattr(setting, "bulk_sync_threshold", DEFAULT_BULK_THRESHOLD)) or DEFAULT_BULK_THRESHOLD,
            "batch_size": cint(getattr(setting, "bulk_sync_batch_size", DEFAULT_BATCH_SIZE)) or DEFAULT_BATCH_SIZE,
        }
    except Exception:
        return {
            "enabled": True,
            "threshold": DEFAULT_BULK_THRESHOLD,
            "batch_size": DEFAULT_BATCH_SIZE,
        }


def get_cache():
    """Get frappe cache instance."""
    return frappe.cache()


def is_bulk_mode_active() -> bool:
    """Check if bulk mode is currently active."""
    cache = get_cache()
    return bool(cache.get_value(BULK_MODE_KEY))


def activate_bulk_mode():
    """Activate bulk mode to queue items instead of syncing immediately."""
    cache = get_cache()
    cache.set_value(BULK_MODE_KEY, "1", expires_in_sec=BULK_COOLDOWN * 3)
    logger.info("Shopware6 Bulk Sync: Bulk mode activated")


def deactivate_bulk_mode():
    """Deactivate bulk mode."""
    cache = get_cache()
    cache.delete_value(BULK_MODE_KEY)
    logger.info("Shopware6 Bulk Sync: Bulk mode deactivated")


def increment_request_count() -> int:
    """
    Increment the request counter and return current count.
    Counter resets after BULK_WINDOW seconds.
    """
    cache = get_cache()
    current_time = time.time()

    # Get last request time and count
    last_request = cache.get_value(LAST_SYNC_REQUEST_KEY)
    count = cint(cache.get_value(REQUEST_COUNT_KEY)) or 0

    if last_request:
        last_time = float(last_request)
        # Reset counter if window has passed
        if current_time - last_time > BULK_WINDOW:
            count = 0

    count += 1
    cache.set_value(REQUEST_COUNT_KEY, str(count), expires_in_sec=BULK_WINDOW * 2)
    cache.set_value(LAST_SYNC_REQUEST_KEY, str(current_time), expires_in_sec=BULK_WINDOW * 2)

    return count


def should_use_bulk_mode() -> bool:
    """
    Determine if bulk mode should be used based on request frequency.
    Returns True if we're in a bulk update scenario.
    """
    settings = get_bulk_sync_settings()

    # Skip if bulk sync is disabled
    if not settings["enabled"]:
        return False

    # Already in bulk mode
    if is_bulk_mode_active():
        # Extend bulk mode timeout
        cache = get_cache()
        cache.set_value(BULK_MODE_KEY, "1", expires_in_sec=BULK_COOLDOWN * 3)
        return True

    # Check request frequency
    count = increment_request_count()

    if count >= settings["threshold"]:
        activate_bulk_mode()
        return True

    return False


def add_to_sync_queue(item_code: str, sync_type: str = "product"):
    """
    Add an item to the sync queue.

    Args:
        item_code: ERPNext Item code
        sync_type: Type of sync - "product" or "properties"
    """
    cache = get_cache()
    queue_key = f"{SYNC_QUEUE_KEY}:{sync_type}"

    # Get current queue
    queue = cache.get_value(queue_key) or []
    if isinstance(queue, str):
        queue = frappe.parse_json(queue) or []

    # Add item if not already in queue
    if item_code not in queue:
        queue.append(item_code)
        cache.set_value(queue_key, frappe.as_json(queue), expires_in_sec=3600)  # 1 hour expiry
        logger.debug(f"Shopware6 Bulk Sync: Added {item_code} to {sync_type} queue. Queue size: {len(queue)}")


def get_sync_queue(sync_type: str = "product") -> List[str]:
    """Get all items in the sync queue."""
    cache = get_cache()
    queue_key = f"{SYNC_QUEUE_KEY}:{sync_type}"
    queue = cache.get_value(queue_key) or []
    if isinstance(queue, str):
        queue = frappe.parse_json(queue) or []
    return queue


def clear_sync_queue(sync_type: str = "product"):
    """Clear the sync queue."""
    cache = get_cache()
    queue_key = f"{SYNC_QUEUE_KEY}:{sync_type}"
    cache.delete_value(queue_key)


def remove_from_queue(item_codes: List[str], sync_type: str = "product"):
    """Remove processed items from the queue."""
    cache = get_cache()
    queue_key = f"{SYNC_QUEUE_KEY}:{sync_type}"
    queue = get_sync_queue(sync_type)
    queue = [item for item in queue if item not in item_codes]
    if queue:
        cache.set_value(queue_key, frappe.as_json(queue), expires_in_sec=3600)
    else:
        cache.delete_value(queue_key)


def acquire_sync_lock(timeout: int = 300) -> bool:
    """
    Acquire a lock to prevent concurrent bulk sync operations.

    Args:
        timeout: Lock timeout in seconds

    Returns:
        True if lock acquired, False otherwise
    """
    cache = get_cache()
    lock_value = cache.get_value(SYNC_LOCK_KEY)

    if lock_value:
        # Check if lock is stale
        lock_time = float(lock_value)
        if time.time() - lock_time > timeout:
            # Lock is stale, acquire it
            cache.set_value(SYNC_LOCK_KEY, str(time.time()), expires_in_sec=timeout)
            return True
        return False

    cache.set_value(SYNC_LOCK_KEY, str(time.time()), expires_in_sec=timeout)
    return True


def release_sync_lock():
    """Release the sync lock."""
    cache = get_cache()
    cache.delete_value(SYNC_LOCK_KEY)


def queue_item_for_sync(doc, method=None):
    """
    Main entry point for queueing items.

    This function should be called from the doc_events hook instead of
    upload_erpnext_item_to_shopware directly.

    It will:
    1. Check if bulk sync is enabled
    2. Check if we're in a bulk update scenario
    3. If yes: queue the item for later batch processing
    4. If no: process immediately (original behavior)
    """
    from ecommerce_integrations.shopware6.product_export import upload_erpnext_item_to_shopware

    item = doc

    # Skip if this came from integration
    if item.flags.from_integration:
        return

    # Skip if explicit flag is set (for bulk operations from external scripts)
    if getattr(frappe.flags, 'skip_shopware_sync', False):
        return

    # Check if Shopware sync is enabled in settings
    try:
        setting = frappe.get_cached_doc(SETTING_DOCTYPE)
        if not setting.is_enabled():
            return

        # Check if item is already synced to determine which setting to check
        from ecommerce_integrations.shopware6.utils import get_shopware_document_id
        shopware_id = get_shopware_document_id("Item", item.name)
        is_new_product = not bool(shopware_id)

        # For NEW products: check upload_erpnext_items setting
        # For EXISTING products: check update_shopware_item_on_update setting
        if is_new_product:
            if not getattr(setting, 'upload_erpnext_items', False):
                return
        else:
            if not getattr(setting, 'update_shopware_item_on_update', True):
                return
    except Exception:
        return

    settings = get_bulk_sync_settings()

    # Always queue during imports or bulk updates - never process directly
    if frappe.flags.in_import or getattr(frappe.flags, 'in_bulk_update', False):
        add_to_sync_queue(item.name, "product")
        if not is_bulk_mode_active():
            activate_bulk_mode()
        return

    # If bulk sync is disabled, use async sync with item_code (not doc object)
    if not settings["enabled"]:
        frappe.enqueue(
            "ecommerce_integrations.shopware6.bulk_sync.sync_single_item_to_shopware",
            queue="short",
            item_code=item.name,
            enqueue_after_commit=True,
        )
        return

    # Check if we should use bulk mode (for mass updates)
    if should_use_bulk_mode():
        add_to_sync_queue(item.name, "product")

        # Schedule bulk processing if not already scheduled
        schedule_bulk_sync_processing()
        return

    # Single update - async with item_code to avoid UI blocking
    frappe.enqueue(
        "ecommerce_integrations.shopware6.bulk_sync.sync_single_item_to_shopware",
        queue="short",
        item_code=item.name,
        enqueue_after_commit=True,
    )


def sync_single_item_to_shopware(item_code: str):
    """
    Sync a single item to Shopware (async wrapper).

    This function loads the item by code and calls the upload function.
    Used by frappe.enqueue because Document objects cannot be serialized.

    Args:
        item_code: The ERPNext Item code to sync
    """
    from ecommerce_integrations.shopware6.product_export import upload_erpnext_item_to_shopware

    logger = get_logger("_sync_single_item")
    try:
        upload_erpnext_item_to_shopware(item_code)
    except Exception as e:
        logger.error(f"Failed to sync item {item_code} to Shopware", exception=e, persist=True)


def queue_properties_for_sync(doc, method=None):
    """
    Queue properties sync for bulk processing.
    """
    from ecommerce_integrations.shopware6.properties import upload_item_properties
    from ecommerce_integrations.shopware6.utils import get_shopware_document_id

    item = doc

    if item.flags.from_integration:
        return

    # Skip if explicit flag is set (for bulk operations from external scripts)
    if getattr(frappe.flags, 'skip_shopware_sync', False):
        return

    # Check if Shopware sync is enabled in settings
    try:
        setting = frappe.get_cached_doc(SETTING_DOCTYPE)
        if not setting.is_enabled():
            return
        # Check if update on item change is enabled
        if not getattr(setting, 'update_shopware_item_on_update', True):
            return
    except Exception:
        return

    # Only queue if item is synced
    shopware_id = get_shopware_document_id("Item", item.name)
    if not shopware_id:
        return

    settings = get_bulk_sync_settings()

    # Always queue during imports or bulk updates - never process directly
    if frappe.flags.in_import or getattr(frappe.flags, 'in_bulk_update', False):
        add_to_sync_queue(item.name, "properties")
        if not is_bulk_mode_active():
            activate_bulk_mode()
        return

    # If bulk sync is disabled, enqueue to prevent blocking
    if not settings["enabled"]:
        frappe.enqueue(
            "ecommerce_integrations.shopware6.properties.upload_item_properties",
            queue="short",
            doc=doc,
            enqueue_after_commit=True,
        )
        return

    if should_use_bulk_mode():
        add_to_sync_queue(item.name, "properties")
        schedule_bulk_sync_processing()
        return

    # Single update - use enqueue (properties.upload_item_properties already enqueues internally)
    upload_item_properties(doc, method)


def queue_item_group_for_sync(doc, method=None):
    """
    Queue Item Group (category) for sync to Shopware.

    This is triggered by the Item Group on_update hook.
    It syncs the category with description, shopware_active status, SEO fields, etc.
    """
    from ecommerce_integrations.shopware6.product_export import sync_item_group_to_shopware

    item_group = doc

    # Skip root categories
    root_categories_to_skip = ["All Item Groups", "Alle Artikelgruppen"]
    if item_group.name in root_categories_to_skip:
        return

    # Skip if explicit flag is set (for bulk operations from external scripts)
    if getattr(frappe.flags, 'skip_shopware_sync', False):
        return

    # Check if Shopware sync is enabled in settings
    try:
        setting = frappe.get_cached_doc(SETTING_DOCTYPE)
        if not setting.is_enabled():
            return
    except Exception:
        return

    # Enqueue to prevent blocking the save operation
    frappe.enqueue(
        "ecommerce_integrations.shopware6.product_export.sync_item_group_to_shopware",
        queue="short",
        item_group_name=item_group.name,
        enqueue_after_commit=True,
    )


def schedule_bulk_sync_processing():
    """Schedule the bulk sync processing job if not already scheduled."""
    # Check if job is already scheduled
    # Wrap in try-except to handle "signal only works in main thread" error
    # which occurs when RQ Job registry cleanup runs outside the main thread
    try:
        jobs = frappe.get_all(
            "RQ Job",
            filters={
                "status": ["in", ["queued", "started"]],
                "job_name": ["like", "%process_bulk_sync_queue%"]
            },
            limit=1
        )
    except ValueError as e:
        if "signal only works in main thread" in str(e):
            # We're not in the main thread, skip the job check and just enqueue
            # The job deduplication will be handled by the queue itself
            jobs = []
        else:
            raise

    if not jobs:
        # Schedule processing after cooldown period
        frappe.enqueue(
            "ecommerce_integrations.shopware6.bulk_sync.process_bulk_sync_queue",
            queue="long",
            timeout=1800,  # 30 minutes
            job_name="shopware6_process_bulk_sync_queue",
            enqueue_after_commit=True,
            at_front=False
        )


def process_bulk_sync_queue():
    """
    Process the bulk sync queue in batches.

    This is called by the scheduler or can be triggered manually.
    """
    from ecommerce_integrations.shopware6.product_export import upload_erpnext_item_to_shopware
    from ecommerce_integrations.shopware6.properties import sync_item_properties_to_shopware
    from ecommerce_integrations.shopware6.connection import get_shopware_client

    # Check if bulk mode is still active (meaning updates are still coming in)
    if is_bulk_mode_active():
        # Re-schedule for later
        frappe.enqueue(
            "ecommerce_integrations.shopware6.bulk_sync.process_bulk_sync_queue",
            queue="long",
            timeout=1800,
            job_name="shopware6_process_bulk_sync_queue_delayed",
            enqueue_after_commit=True,
            at_front=False
        )
        logger.info("Shopware6 Bulk Sync: Bulk mode still active, rescheduling...")
        return

    # Acquire lock
    if not acquire_sync_lock():
        logger.warning("Shopware6 Bulk Sync: Could not acquire lock, another job is running")
        return

    try:
        setting = frappe.get_doc(SETTING_DOCTYPE)
        if not setting.is_enabled():
            logger.info("Shopware6 Bulk Sync: Integration disabled")
            return

        # Process product queue
        product_queue = get_sync_queue("product")
        if product_queue:
            logger.info(f"Shopware6 Bulk Sync: Processing {len(product_queue)} products")
            process_product_batch(product_queue)

        # Process properties queue
        properties_queue = get_sync_queue("properties")
        if properties_queue:
            logger.info(f"Shopware6 Bulk Sync: Processing {len(properties_queue)} property syncs")
            process_properties_batch(properties_queue)

        # Process price queue
        price_queue = get_sync_queue("price")
        if price_queue:
            logger.info(f"Shopware6 Bulk Sync: Processing {len(price_queue)} price updates")
            process_price_batch(price_queue)

        logger.info("Shopware6 Bulk Sync: Queue processing completed")

    except Exception as e:
        logger = get_logger("check_and_process_queue")
        logger.error("Shopware6 Bulk Sync Error", exception=e, persist=True)
    finally:
        release_sync_lock()
        deactivate_bulk_mode()


def process_product_batch(item_codes: List[str]):
    """
    Process a batch of products using optimized batch upload.

    Uses Shopware Sync API for simple items (100+ products per request).
    Templates and variants still use sequential processing due to dependencies.
    """
    from ecommerce_integrations.shopware6.product_export import (
        upload_template_item_to_shopware,
        upload_variant_item_to_shopware,
    )
    from ecommerce_integrations.shopware6.export.batch_uploader import BatchProductUploader
    from ecommerce_integrations.shopware6.connection import get_shopware_client
    from ecommerce_integrations.shopware6.utils import get_shopware_document_id

    settings = get_bulk_sync_settings()
    batch_size = settings["batch_size"]
    logger = get_logger("process_product_batch")

    processed = []
    errors = []

    # Categorize items by type using a single batch query
    item_types = frappe.get_all(
        "Item",
        filters={"name": ["in", item_codes]},
        fields=["name", "has_variants", "variant_of"]
    )
    item_type_map = {i.name: i for i in item_types}

    templates = []
    variants = []
    simple_item_codes = []

    for item_code in item_codes:
        item_info = item_type_map.get(item_code)
        if not item_info:
            errors.append((item_code, "Item not found"))
            continue

        if item_info.has_variants:
            templates.append(item_code)
        elif item_info.variant_of:
            variants.append(item_code)
        else:
            simple_item_codes.append(item_code)

    client = get_shopware_client()
    if not client:
        logger.error("Could not get Shopware client", persist=False)
        return

    # 1. Process templates using BATCH UPLOADER (Sync API)
    if templates:
        logger.info(f"Processing {len(templates)} template items via Sync API...")
        try:
            uploader = BatchProductUploader()
            result = uploader.upload_templates(templates, skip_images=True)

            # Track processed items
            processed.extend(result.processed_item_codes)

            for error in result.errors:
                errors.append((error.get("item_code", "unknown"), error.get("error", "Unknown error")))

            logger.info(
                f"Template batch upload: {result.success} success, {result.failed} failed, {result.skipped} skipped"
            )
        except Exception as e:
            error_msg = str(e)[:500]
            logger.error(f"Template batch uploader failed: {error_msg}", exception=e)
            # Fallback: sequential processing
            for item_code in templates:
                try:
                    item = frappe.get_doc("Item", item_code)
                    item.flags.from_integration = False
                    upload_template_item_to_shopware(client, item)
                    processed.append(item_code)
                except Exception as e2:
                    errors.append((item_code, str(e2)[:200]))

    # 2. Process simple items using BATCH UPLOADER (Sync API)
    if simple_item_codes:
        logger.info(f"Processing {len(simple_item_codes)} simple items via Sync API...")
        try:
            uploader = BatchProductUploader()
            result = uploader.upload_items(simple_item_codes, skip_images=True)

            # Use processed_item_codes (ERPNext item codes, not Shopware IDs)
            processed.extend(result.processed_item_codes)

            for error in result.errors:
                errors.append((error.get("item_code", "unknown"), error.get("error", "Unknown error")))

            logger.info(
                f"Batch upload: {result.success} success, {result.failed} failed, {result.skipped} skipped"
            )
        except Exception as e:
            error_msg = str(e)[:500]
            logger.error(f"Batch uploader failed: {error_msg}", exception=e)
            # Fallback: mark all as errors
            for item_code in simple_item_codes:
                errors.append((item_code, f"Batch failed: {error_msg[:100]}"))

    # 3. Process variants using BATCH UPLOADER (Sync API) - much faster!
    if variants:
        logger.info(f"Processing {len(variants)} variant items via Sync API...")
        try:
            uploader = BatchProductUploader()
            result = uploader.upload_variants(variants, skip_images=True)

            # Track processed items
            processed.extend(result.processed_item_codes)

            for error in result.errors:
                errors.append((error.get("item_code", "unknown"), error.get("error", "Unknown error")))

            logger.info(
                f"Variant batch upload: {result.success} success, {result.failed} failed, {result.skipped} skipped"
            )
        except Exception as e:
            error_msg = str(e)[:500]
            logger.error(f"Variant batch uploader failed: {error_msg}", exception=e)
            # Fallback: mark all as errors
            for item_code in variants:
                errors.append((item_code, f"Batch failed: {error_msg[:100]}"))

    # Remove processed items from queue
    remove_from_queue(processed, "product")

    # Log summary
    logger.info(
        f"Bulk Sync complete: {len(processed)} processed, {len(errors)} errors "
        f"(templates: {len(templates)}, simple: {len(simple_item_codes)}, variants: {len(variants)})"
    )

    if errors:
        failed_items = [code for code, _ in errors]
        remove_from_queue(failed_items, "product")

        error_summary = "\n".join([f"{code}: {str(err)[:80]}" for code, err in errors[:10]])
        logger.error(f"Sync errors:\n{error_summary}", persist=False)


def process_properties_batch(item_codes: List[str]):
    """Process a batch of property syncs."""
    from ecommerce_integrations.shopware6.properties import sync_item_properties_to_shopware

    settings = get_bulk_sync_settings()
    batch_size = settings["batch_size"]

    processed = []
    errors = []

    for batch_start in range(0, len(item_codes), batch_size):
        batch = item_codes[batch_start:batch_start + batch_size]
        for item_code in batch:
            try:
                sync_item_properties_to_shopware(item_code)
                processed.append(item_code)
            except Exception as e:
                errors.append((item_code, str(e)))
                get_logger().error(f"Property sync failed for {item_code}: {e}", persist=False)

        frappe.db.commit()
        time.sleep(BATCH_DELAY)

    remove_from_queue(processed, "properties")

    logger.info(
        f"Shopware6 Bulk Sync: Processed {len(processed)} property syncs, {len(errors)} errors"
    )


def process_price_batch(item_codes: List[str]):
    """Process a batch of price updates."""
    from ecommerce_integrations.shopware6.product_export import update_item_price_in_shopware

    settings = get_bulk_sync_settings()
    batch_size = settings["batch_size"]

    processed = []
    errors = []

    for batch_start in range(0, len(item_codes), batch_size):
        batch = item_codes[batch_start:batch_start + batch_size]
        for item_code in batch:
            try:
                result = update_item_price_in_shopware(item_code=item_code)
                if result.get("success"):
                    processed.append(item_code)
                else:
                    errors.append((item_code, result.get("message", "Unknown error")))
            except Exception as e:
                errors.append((item_code, str(e)))
                get_logger().error(f"Price sync failed for {item_code}: {e}", persist=False)

        frappe.db.commit()
        time.sleep(BATCH_DELAY)

    remove_from_queue(processed, "price")

    logger.info(
        f"Shopware6 Bulk Sync: Processed {len(processed)} price updates, {len(errors)} errors"
    )

    if errors:
        error_summary = "\n".join([f"{code}: {err}" for code, err in errors[:20]])
        get_logger().error(f"Shopware6 Price Sync Errors:\n{error_summary}", persist=False)


def is_processing() -> bool:
    """Check if bulk sync is currently processing."""
    cache = get_cache()
    return bool(cache.get_value(SYNC_LOCK_KEY))


@frappe.whitelist()
def get_queue_status():
    """Get current queue status for monitoring.

    Returns format similar to RAG sync for consistency:
    - queue_size: Total items in queue
    - bulk_mode_active: Whether bulk mode is on
    - processing: Whether sync is currently running
    """
    product_queue = get_sync_queue("product")
    properties_queue = get_sync_queue("properties")
    price_queue = get_sync_queue("price")

    return {
        "queue_size": len(product_queue) + len(properties_queue) + len(price_queue),
        "bulk_mode_active": is_bulk_mode_active(),
        "processing": is_processing(),
        "product_queue_size": len(product_queue),
        "properties_queue_size": len(properties_queue),
        "price_queue_size": len(price_queue),
        "product_queue": product_queue[:10],  # First 10 items
        "properties_queue": properties_queue[:10],
        "price_queue": price_queue[:10],
    }


@frappe.whitelist()
def force_process_queue():
    """Manually trigger queue processing."""
    deactivate_bulk_mode()
    frappe.enqueue(
        "ecommerce_integrations.shopware6.bulk_sync.process_bulk_sync_queue",
        queue="long",
        timeout=1800,
        job_name="shopware6_force_bulk_sync",
        now=True
    )
    return {"status": "Processing started"}


@frappe.whitelist()
def clear_all_queues():
    """Clear all sync queues (admin function)."""
    clear_sync_queue("product")
    clear_sync_queue("properties")
    clear_sync_queue("price")
    deactivate_bulk_mode()
    release_sync_lock()
    return {"status": "All queues cleared"}


def check_and_process_queue():
    """
    Scheduled task to check and process the sync queue.

    This runs on "all" scheduler events (every minute) and:
    1. Checks if there are items in the queue
    2. Checks if bulk mode is inactive (no new updates coming in)
    3. If both conditions are met, triggers queue processing
    """
    # Skip if bulk mode is active (updates still coming in)
    if is_bulk_mode_active():
        return

    # Check if there are items to process
    product_queue = get_sync_queue("product")
    properties_queue = get_sync_queue("properties")
    price_queue = get_sync_queue("price")

    if not product_queue and not properties_queue and not price_queue:
        return

    # Check if a sync job is already running
    cache = get_cache()
    lock_value = cache.get_value(SYNC_LOCK_KEY)
    if lock_value:
        return

    # Trigger queue processing
    logger.info(
        f"Shopware6 Bulk Sync: Scheduler triggering queue processing. "
        f"Products: {len(product_queue)}, Properties: {len(properties_queue)}, Prices: {len(price_queue)}"
    )

    frappe.enqueue(
        "ecommerce_integrations.shopware6.bulk_sync.process_bulk_sync_queue",
        queue="long",
        timeout=1800,
        job_name="shopware6_scheduled_bulk_sync",
        enqueue_after_commit=True
    )


def queue_price_for_sync(doc, method=None):
    """
    Queue price sync when an Item Price is changed.

    This function is called from the doc_events hook for Item Price.
    It finds the item associated with the price and queues it for price sync.
    """
    from ecommerce_integrations.shopware6.utils import get_shopware_document_id

    item_price = doc

    # Skip if this came from integration
    if item_price.flags.from_integration:
        return

    # Skip if explicit flag is set
    if getattr(frappe.flags, 'skip_shopware_sync', False):
        return

    # Check if Shopware sync is enabled in settings
    try:
        setting = frappe.get_cached_doc(SETTING_DOCTYPE)
        if not setting.is_enabled():
            return
    except Exception:
        return

    # Get item code from Item Price
    item_code = item_price.item_code
    if not item_code:
        return

    # Only sync if item is already synced to Shopware
    shopware_id = get_shopware_document_id("Item", item_code)
    if not shopware_id:
        return

    # Check if this is the selling price list
    selling_price_list = setting.get("price_list") or frappe.db.get_single_value("Selling Settings", "selling_price_list")
    if item_price.price_list != selling_price_list:
        return

    logger.info(f"Shopware6: Queueing price sync for item {item_code} (price list: {item_price.price_list})")

    settings = get_bulk_sync_settings()

    # Always queue during imports or bulk updates
    if frappe.flags.in_import or getattr(frappe.flags, 'in_bulk_update', False):
        add_to_sync_queue(item_code, "price")
        if not is_bulk_mode_active():
            activate_bulk_mode()
        return

    # If bulk sync is disabled, process immediately
    if not settings["enabled"]:
        frappe.enqueue(
            "ecommerce_integrations.shopware6.product_export.update_item_price_in_shopware",
            queue="short",
            item_code=item_code,
            enqueue_after_commit=True,
        )
        return

    # Check if we should use bulk mode
    if should_use_bulk_mode():
        add_to_sync_queue(item_code, "price")
        schedule_bulk_sync_processing()
        return

    # Single update - enqueue to prevent blocking
    frappe.enqueue(
        "ecommerce_integrations.shopware6.product_export.update_item_price_in_shopware",
        queue="short",
        item_code=item_code,
        enqueue_after_commit=True,
    )
