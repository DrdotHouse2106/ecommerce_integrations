"""
Medusa Bulk Sync Queue

Queues item changes and processes them in batches of 10.
Prevents overloading Medusa during bulk ERPNext updates.

Flow:
1. Item on_update/after_insert -> queue_item_for_sync() adds to Redis queue
2. Scheduler (every minute) -> check_and_process_queue() picks up queued items
3. Processes in batches of 10 with a short delay between batches
"""

import time

import frappe

from ecommerce_integrations.controllers.bulk_sync_base import (
	BulkModeTracker,
	acquire_lock,
	release_lock,
)
from ecommerce_integrations.medusa.constants import PRODUCT_ID_FIELD, SETTING_DOCTYPE
from ecommerce_integrations.medusa.utils import is_medusa_enabled

QUEUE_KEY = "medusa:sync_queue:product"
PRICE_QUEUE_KEY = "medusa:sync_queue:price"
LOCK_KEY = "medusa:sync_lock"

BATCH_SIZE = 10
BULK_THRESHOLD = 5
BULK_WINDOW = 2       # seconds
BULK_COOLDOWN = 10    # seconds before processing starts
BATCH_DELAY = 0.5     # seconds between batches

_TRACKER = BulkModeTracker(
	key_prefix="medusa",
	threshold=BULK_THRESHOLD,
	window=BULK_WINDOW,
	cooldown=BULK_COOLDOWN,
)


def queue_item_for_sync(doc, method=None):
    """Hook: queue an Item change for Medusa sync."""
    if not is_medusa_enabled():
        return
    if doc.flags.from_integration:
        return

    setting = frappe.get_cached_doc(SETTING_DOCTYPE)
    has_medusa_id = bool(doc.get(PRODUCT_ID_FIELD))

    # disabled-state toggles must always sync if the item is already in Medusa,
    # so the mirror reflects ERPNext (disabled → status=draft, re-enabled → published).
    is_disabled_change = (
        method == "on_update"
        and has_medusa_id
        and getattr(doc, "has_value_changed", lambda _f: False)("disabled")
    )

    if method == "after_insert" and not setting.upload_erpnext_items:
        return
    if method == "on_update" and not setting.sync_item_on_update and not is_disabled_change:
        return

    # New items that are already disabled don't need to land in Medusa as drafts.
    if doc.get("disabled") and not has_medusa_id:
        return

    _add_to_queue(doc.name, QUEUE_KEY)

    if _should_activate_bulk_mode():
        return
    frappe.enqueue(
        "ecommerce_integrations.medusa.bulk_sync.process_queue",
        queue="short",
        job_name="medusa_single_sync",
        enqueue_after_commit=True,
    )


def queue_price_for_sync(doc, method=None):
    """Hook: queue an Item Price change for Medusa price sync."""
    if not is_medusa_enabled():
        return
    if doc.flags.from_integration:
        return

    item_code = doc.item_code
    if not item_code:
        return

    # Skip price lists not relevant to Medusa
    setting = frappe.get_cached_doc(SETTING_DOCTYPE)
    relevant_lists = {setting.default_selling_price_list}
    if getattr(setting, "list_price_price_list", None):
        relevant_lists.add(setting.list_price_price_list)
    if doc.price_list not in relevant_lists:
        return

    # Only queue if item is synced to Medusa
    from ecommerce_integrations.medusa.constants import VARIANT_ID_FIELD
    if not frappe.db.get_value("Item", item_code, VARIANT_ID_FIELD):
        return

    _add_to_queue(item_code, PRICE_QUEUE_KEY)

    if _should_activate_bulk_mode():
        return
    frappe.enqueue(
        "ecommerce_integrations.medusa.bulk_sync.process_queue",
        queue="short",
        job_name="medusa_single_price_sync",
        enqueue_after_commit=True,
    )


def check_and_process_queue():
    """Scheduler task (runs every minute). Processes queue if bulk mode is off."""
    if _is_bulk_mode():
        return

    product_queue = _get_queue(QUEUE_KEY)
    price_queue = _get_queue(PRICE_QUEUE_KEY)
    if not product_queue and not price_queue:
        return

    if frappe.cache.get_value(LOCK_KEY):
        return

    frappe.enqueue(
        "ecommerce_integrations.medusa.bulk_sync.process_queue",
        queue="long",
        timeout=1800,
        job_name="medusa_bulk_sync",
        enqueue_after_commit=True,
    )


def process_queue():
    """Process queued items in batches."""
    if not _acquire_lock():
        return

    try:
        _process_product_queue()
        _process_price_queue()
    finally:
        _release_lock()


def _process_product_queue():
    from ecommerce_integrations.medusa.product_export import MedusaProductExporter

    queue = _get_queue(QUEUE_KEY)
    if not queue:
        return

    total = len(queue)
    processed = 0
    errors = 0

    for i in range(0, total, BATCH_SIZE):
        batch = queue[i:i + BATCH_SIZE]

        for item_code in batch:
            try:
                MedusaProductExporter(item_code).export()
                processed += 1
            except Exception as e:
                errors += 1
                frappe.log_error(f"Medusa bulk sync failed: {item_code}", str(e))

        # Remove processed batch from the live queue (may have new items added since snapshot)
        _remove_from_queue(batch, QUEUE_KEY)
        frappe.db.commit()

        if i + BATCH_SIZE < total:
            time.sleep(BATCH_DELAY)

    if total:
        frappe.logger("medusa").info(
            f"Medusa Bulk Sync: {processed}/{total} products synced, {errors} errors"
        )


def _process_price_queue():
    from ecommerce_integrations.medusa.product_export import MedusaProductExporter

    queue = _get_queue(PRICE_QUEUE_KEY)
    if not queue:
        return

    total = len(queue)
    processed = 0
    errors = 0

    for i in range(0, total, BATCH_SIZE):
        batch = queue[i:i + BATCH_SIZE]

        for item_code in batch:
            try:
                MedusaProductExporter(item_code).update_price()
                processed += 1
            except Exception as e:
                errors += 1
                frappe.log_error(f"Medusa price sync failed: {item_code}", str(e))

        _remove_from_queue(batch, PRICE_QUEUE_KEY)
        frappe.db.commit()

        if i + BATCH_SIZE < total:
            time.sleep(BATCH_DELAY)

    if total:
        frappe.logger("medusa").info(
            f"Medusa Bulk Sync: {processed}/{total} prices synced, {errors} errors"
        )


# ── Queue helpers ─────────────────────────────────────────────

def _add_to_queue(item_code, queue_key):
    """Add item to queue. Uses a set internally to avoid duplicates safely."""
    cache = frappe.cache
    queue = _get_queue(queue_key)
    items = set(queue)
    if item_code not in items:
        queue.append(item_code)
        cache.set_value(queue_key, frappe.as_json(queue), expires_in_sec=3600)


def _get_queue(queue_key):
    queue = frappe.cache.get_value(queue_key) or []
    if isinstance(queue, str):
        queue = frappe.parse_json(queue) or []
    return queue


def _remove_from_queue(item_codes, queue_key):
    """Remove items from the live queue (re-reads to preserve items added during processing)."""
    cache = frappe.cache
    to_remove = set(item_codes)
    queue = _get_queue(queue_key)
    remaining = [i for i in queue if i not in to_remove]
    if remaining:
        cache.set_value(queue_key, frappe.as_json(remaining), expires_in_sec=3600)
    else:
        cache.delete_value(queue_key)


# ── Bulk mode + lock helpers ──────────────────────────────────
# Delegate to controllers.bulk_sync_base so all channels share the same
# atomic-lock and request-frequency semantics.

def _is_bulk_mode():
    return _TRACKER.is_active()


def _should_activate_bulk_mode():
    """Check request frequency. Returns True if bulk mode is on."""
    return _TRACKER.should_activate()


def _acquire_lock(timeout=300):
    return acquire_lock(LOCK_KEY, ttl_seconds=timeout)


def _release_lock():
    release_lock(LOCK_KEY)
