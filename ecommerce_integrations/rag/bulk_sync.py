# Copyright (c) 2024, Frappe and contributors
# For license information, please see license.txt

import json
import time

import frappe
from frappe.utils import now_datetime

from ecommerce_integrations.controllers.bulk_sync_base import (
    BulkModeTracker,
    acquire_lock,
    release_lock,
)
from ecommerce_integrations.rag.services.access import require_rag_admin

# Redis Keys. Bulk-mode / request-counter keys are owned by BulkModeTracker
# (controllers.bulk_sync_base), derived from the "rag" prefix.
SYNC_QUEUE_KEY = "rag:sync_queue"
SYNC_LOCK_KEY = "rag:sync_lock"

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


def _get_tracker() -> BulkModeTracker:
    """Build a ``BulkModeTracker`` with the current RAG Settings threshold.

    Threshold is read from RAG Setting on every call so the admin can tune
    it without a worker restart. Other window/cooldown are fixed constants.
    """
    return BulkModeTracker(
        key_prefix="rag",
        threshold=get_bulk_threshold(),
        window=REQUEST_WINDOW,
        cooldown=BULK_COOLDOWN,
    )


def should_use_bulk_mode():
    """Return True if bulk mode should be active.

    Bulk mode is determined by request frequency: more than
    ``RAG Setting.bulk_sync_threshold`` operations within ``REQUEST_WINDOW``
    seconds activates it. The activated key auto-expires ``BULK_COOLDOWN``
    seconds after the last operation.
    """
    if not is_bulk_sync_enabled():
        return False
    return _get_tracker().should_activate()


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
    """Clear the queue.

    Uses ``delete_value`` (site-prefixed key) because ``add_to_queue`` /
    ``get_queue_items`` / ``remove_from_queue`` all go through Frappe's
    namespaced ``sadd`` / ``smembers`` / ``srem``. ``redis.delete()`` on
    the raw key would no-op silently against a key that lives under the
    site prefix — a pre-refactor bug uncovered by the bulk-sync tests.
    """
    get_redis().delete_value(SYNC_QUEUE_KEY)


def remove_from_queue(item_code: str):
    """Remove item from queue"""
    redis = get_redis()
    items = get_queue_items()
    for item in items:
        if item.get("item_code") == item_code:
            redis.srem(SYNC_QUEUE_KEY, json.dumps(item))


_BULK_SYNC_JOB_NAME = "rag_process_bulk_sync_queue"


def schedule_bulk_sync_processing():
    """Idempotently schedule a bulk-processing job.

    Previously walked every queued/started RQ job across every site and
    substring-matched ``"process_bulk_sync_queue" in str(job)`` — racy and
    cross-site leaky. Now we identify the job by a stable ``job_name`` and
    query the ``RQ Job`` doctype for an exact match.
    """
    try:
        existing = frappe.get_all(
            "RQ Job",
            filters={
                "status": ("in", ("queued", "started")),
                "job_name": _BULK_SYNC_JOB_NAME,
            },
            limit=1,
        )
    except ValueError as e:
        # RQ Job's registry cleanup runs ``signal.signal(...)`` which raises
        # "signal only works in main thread" when invoked from a worker. If
        # that bubbles out, skip the dedup check — the next scheduler tick
        # (check_and_process_queue, every minute) will pick the work up.
        if "signal only works in main thread" not in str(e):
            raise
        existing = []

    if existing:
        return

    frappe.enqueue(
        "ecommerce_integrations.rag.bulk_sync.process_bulk_sync_queue",
        queue="long",
        job_name=_BULK_SYNC_JOB_NAME,
        enqueue_after_commit=True,
        at_front=False,
    )


def process_bulk_sync_queue():
    """Process the bulk queue"""
    # Bulk mode still active means the user is still queuing items. Skip
    # this run; ``check_and_process_queue`` (scheduler, every minute) will
    # pick this back up once BULK_MODE_KEY expires.
    if _get_tracker().is_active():
        return

    # Check if still items in queue
    items = get_queue_items()
    if not items:
        return

    # Atomically acquire the lock. The previous implementation did a
    # non-atomic get-then-set, so two workers reaching this point
    # concurrently could both observe "no lock" and both proceed —
    # producing duplicate batches.
    if not acquire_lock(SYNC_LOCK_KEY, ttl_seconds=600):
        return

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
        release_lock(SYNC_LOCK_KEY)
        _get_tracker().deactivate()


def check_and_process_queue():
    """Scheduler job: Check queue every 60 seconds"""
    if not is_rag_enabled():
        return

    # Don't auto-process if paused
    if is_auto_sync_paused():
        return

    # Only if not in active bulk mode
    if _get_tracker().is_active():
        return

    items = get_queue_items()
    if items:
        process_bulk_sync_queue()


@frappe.whitelist()
def force_process_queue():
    """Manual trigger for queue processing"""
    require_rag_admin()
    frappe.enqueue(
        process_bulk_sync_queue,
        queue="long"
    )
    return {"status": "Queue processing started"}


@frappe.whitelist()
def get_queue_status():
    """Get queue status for UI"""
    require_rag_admin()
    items = get_queue_items()

    # ``processing`` reads the raw SYNC_LOCK_KEY (no Frappe namespace) so it
    # sees the same value acquire_lock writes via SET NX EX. bulk_mode_active
    # goes through the BulkModeTracker so it stays consistent with the
    # site-prefixed key the tracker writes.
    return {
        "queue_size": len(items),
        "bulk_mode_active": _get_tracker().is_active(),
        "processing": bool(get_redis().get(SYNC_LOCK_KEY)),
        "auto_sync_paused": is_auto_sync_paused(),
    }


@frappe.whitelist()
def clear_sync_queue():
    """Clear the sync queue (for manual cleanup)"""
    require_rag_admin()
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
