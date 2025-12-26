# Copyright (c) 2024, Frappe and contributors
# For license information, please see license.txt

import json
import time
import frappe
from frappe.utils import now_datetime

# Redis Keys
SYNC_QUEUE_KEY = "rag:sync_queue"
BULK_MODE_KEY = "rag:bulk_mode"
SYNC_LOCK_KEY = "rag:sync_lock"
REQUEST_COUNT_KEY = "rag:request_count"
LAST_REQUEST_KEY = "rag:last_request"

# Timing
BULK_COOLDOWN = 10  # seconds
REQUEST_WINDOW = 2  # seconds


def get_redis():
    """Get Redis connection"""
    return frappe.cache()


def is_rag_enabled():
    """Check if RAG sync is enabled"""
    try:
        settings = frappe.get_single("RAG Setting")
        return settings.enabled
    except Exception:
        return False


def is_bulk_sync_enabled():
    """Check if bulk sync is enabled"""
    try:
        settings = frappe.get_single("RAG Setting")
        return settings.enable_bulk_sync
    except Exception:
        return False


def is_auto_sync_paused():
    """Check if auto sync is paused (items still queue but don't process automatically)"""
    try:
        settings = frappe.get_single("RAG Setting")
        return getattr(settings, 'pause_auto_sync', False)
    except Exception:
        return False


def get_bulk_threshold():
    """Get threshold for bulk mode activation"""
    settings = frappe.get_single("RAG Setting")
    return settings.bulk_sync_threshold or 5


def get_batch_size():
    """Get batch size for processing"""
    settings = frappe.get_single("RAG Setting")
    return settings.bulk_sync_batch_size or 50


def should_use_bulk_mode():
    """Check if bulk mode should be activated"""
    if not is_bulk_sync_enabled():
        return False

    redis = get_redis()
    current_time = time.time()

    # Increment request counter
    last_request = redis.get(LAST_REQUEST_KEY)
    if last_request:
        try:
            last_time = float(last_request)
            if current_time - last_time <= REQUEST_WINDOW:
                count = redis.incr(REQUEST_COUNT_KEY)
            else:
                redis.set(REQUEST_COUNT_KEY, 1)
                count = 1
        except (ValueError, TypeError):
            redis.set(REQUEST_COUNT_KEY, 1)
            count = 1
    else:
        redis.set(REQUEST_COUNT_KEY, 1)
        count = 1

    redis.set(LAST_REQUEST_KEY, str(current_time))

    # Check threshold
    threshold = get_bulk_threshold()
    if count >= threshold:
        redis.set(BULK_MODE_KEY, "1", ex=BULK_COOLDOWN + 5)
        return True

    # Check if already in bulk mode
    return redis.get(BULK_MODE_KEY) == "1"


def queue_item_for_sync(doc, method=None):
    """Queue Item for RAG sync (Hook entry point)"""
    # Skip if explicit flag is set (for bulk operations from external scripts)
    if getattr(frappe.flags, 'skip_shopware_sync', False):
        return

    if not is_rag_enabled():
        return

    settings = frappe.get_single("RAG Setting")

    # Check filters
    if settings.sync_only_sellable and not doc.is_sales_item:
        return

    if settings.item_group_filter:
        if not is_in_item_group(doc.item_group, settings.item_group_filter):
            return

    # Check if auto sync is paused - queue items but don't process
    if is_auto_sync_paused():
        add_to_queue(doc.item_code)
        # Don't schedule processing - user must manually trigger
        return

    # Bulk or Direct?
    if should_use_bulk_mode():
        add_to_queue(doc.item_code)
        schedule_bulk_sync_processing()
    else:
        # Direct sync
        from .product_export import upload_item_to_rag
        frappe.enqueue(
            upload_item_to_rag,
            item_code=doc.item_code,
            queue="short",
            enqueue_after_commit=True
        )


def add_to_queue(item_code: str):
    """Add item to queue"""
    redis = get_redis()
    queue_data = {
        "item_code": item_code,
        "queued_at": now_datetime().isoformat()
    }
    redis.sadd(SYNC_QUEUE_KEY, json.dumps(queue_data))


def get_queue_items() -> list:
    """Get all items from queue"""
    redis = get_redis()
    items = redis.smembers(SYNC_QUEUE_KEY) or []
    result = []
    for item in items:
        try:
            if isinstance(item, bytes):
                item = item.decode('utf-8')
            result.append(json.loads(item))
        except (json.JSONDecodeError, TypeError):
            continue
    return result


def clear_queue():
    """Clear the queue"""
    redis = get_redis()
    redis.delete(SYNC_QUEUE_KEY)


def remove_from_queue(item_code: str):
    """Remove item from queue"""
    redis = get_redis()
    items = get_queue_items()
    for item in items:
        if item.get("item_code") == item_code:
            redis.srem(SYNC_QUEUE_KEY, json.dumps(item))


def schedule_bulk_sync_processing():
    """Schedule bulk processing after cooldown"""
    # Check if job already exists
    from frappe.utils.background_jobs import get_jobs
    jobs = get_jobs()

    for site_jobs in jobs.values():
        for job in site_jobs:
            if "process_bulk_sync_queue" in str(job):
                return  # Job already scheduled

    frappe.enqueue(
        "ecommerce_integrations.rag.bulk_sync.process_bulk_sync_queue",
        queue="long",
        at_front=False,
        enqueue_after_commit=True
    )


def process_bulk_sync_queue():
    """Process the bulk queue"""
    redis = get_redis()

    # Check lock
    if redis.get(SYNC_LOCK_KEY):
        return

    # Wait for cooldown
    time.sleep(BULK_COOLDOWN)

    # Check if still items in queue
    items = get_queue_items()
    if not items:
        return

    # Set lock (10 minutes max)
    redis.set(SYNC_LOCK_KEY, "1", ex=600)

    try:
        from .product_export import upload_items_batch_to_rag

        batch_size = get_batch_size()
        item_codes = list(set([item["item_code"] for item in items]))

        frappe.logger().info(f"RAG Bulk Sync: Processing {len(item_codes)} items")

        # Process in batches
        for i in range(0, len(item_codes), batch_size):
            batch = item_codes[i:i + batch_size]

            try:
                upload_items_batch_to_rag(batch)
                frappe.db.commit()
            except Exception as e:
                frappe.log_error(
                    message=str(e),
                    title=f"RAG Bulk Sync Error - Batch {i}"
                )

            time.sleep(1)  # Rate limiting

        # Clear queue
        clear_queue()

        # Update stats
        settings = frappe.get_single("RAG Setting")
        settings.last_sync = now_datetime()
        settings.save(ignore_permissions=True)
        frappe.db.commit()

        frappe.logger().info(f"RAG Bulk Sync: Completed {len(item_codes)} items")

    except Exception as e:
        frappe.log_error(message=str(e), title="RAG Bulk Sync Error")
    finally:
        redis.delete(SYNC_LOCK_KEY)
        redis.delete(BULK_MODE_KEY)


def check_and_process_queue():
    """Scheduler job: Check queue every 60 seconds"""
    if not is_rag_enabled():
        return

    # Don't auto-process if paused
    if is_auto_sync_paused():
        return

    redis = get_redis()

    # Only if not in active bulk mode
    if redis.get(BULK_MODE_KEY):
        return

    items = get_queue_items()
    if items:
        process_bulk_sync_queue()


@frappe.whitelist()
def force_process_queue():
    """Manual trigger for queue processing"""
    frappe.enqueue(
        process_bulk_sync_queue,
        queue="long"
    )
    return {"status": "Queue processing started"}


@frappe.whitelist()
def get_queue_status():
    """Get queue status for UI"""
    redis = get_redis()
    items = get_queue_items()

    bulk_mode = redis.get(BULK_MODE_KEY)
    if isinstance(bulk_mode, bytes):
        bulk_mode = bulk_mode.decode('utf-8')

    processing = redis.get(SYNC_LOCK_KEY)
    if isinstance(processing, bytes):
        processing = processing.decode('utf-8')

    return {
        "queue_size": len(items),
        "bulk_mode_active": bulk_mode == "1",
        "processing": processing == "1",
        "auto_sync_paused": is_auto_sync_paused()
    }


@frappe.whitelist()
def clear_sync_queue():
    """Clear the sync queue (for manual cleanup)"""
    clear_queue()
    return {"status": "Queue cleared"}


def is_in_item_group(item_group, filter_group):
    """Check if Item Group is in filter (including subgroups)"""
    if not item_group or not filter_group:
        return False

    if item_group == filter_group:
        return True

    # Check parent chain
    current = frappe.get_value("Item Group", item_group, "parent_item_group")
    while current:
        if current == filter_group:
            return True
        current = frappe.get_value("Item Group", current, "parent_item_group")

    return False
