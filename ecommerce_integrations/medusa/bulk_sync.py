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
from ecommerce_integrations.medusa.connection import optional_session
from ecommerce_integrations.medusa.constants import SETTING_DOCTYPE
from ecommerce_integrations.medusa.utils import (
    create_medusa_log,
    get_medusa_variant_id,
    is_medusa_enabled,
    is_synced_to_medusa,
    update_medusa_log,
)

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
    """Hook: queue an Item change for Medusa sync.

    Phase-Future refactor: only enqueue when at least one ACTIVE
    Product Sync's scope covers this Item. The old behaviour ("push
    every ``Item.save()``") is unsafe at scale — it produces thousands
    of pointless pushes for items the operator does not actually want
    synced. The ``Ecommerce Product Sync`` doctype is now the explicit
    opt-in for "which Items get pushed".
    """
    if not is_medusa_enabled():
        return
    if doc.flags.from_integration:
        return

    if not _item_in_any_active_sync(doc):
        return

    setting = frappe.get_cached_doc(SETTING_DOCTYPE)
    has_medusa_id = is_synced_to_medusa(doc.name)

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
    # job_id + deduplicate=True collapse rapid double-saves of the same Item
    # into a single queued job. Without this, every save spawns a parallel
    # sync job; the queue-based work below would still dedup items, but the
    # job overhead piled up.
    frappe.enqueue(
        "ecommerce_integrations.medusa.bulk_sync.process_queue",
        queue="short",
        job_name="medusa_single_sync",
        enqueue_after_commit=True,
        job_id=f"medusa_sync_item:{doc.name}",
        deduplicate=True,
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

    # Only queue if the item has a synced variant in Medusa
    if not get_medusa_variant_id(item_code):
        return

    _add_to_queue(item_code, PRICE_QUEUE_KEY)

    if _should_activate_bulk_mode():
        return
    frappe.enqueue(
        "ecommerce_integrations.medusa.bulk_sync.process_queue",
        queue="short",
        job_name="medusa_single_price_sync",
        enqueue_after_commit=True,
        job_id=f"medusa_price:{item_code}",
        deduplicate=True,
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
    from ecommerce_integrations.medusa.product_export import (
        MedusaProductExporter,
        _build_medusa_sku_map,
    )

    queue = _get_queue(QUEUE_KEY)
    if not queue:
        return

    total = len(queue)
    processed = 0
    errors = 0

    log_name = create_medusa_log(
        request_type="bulk_sync.process_queue",
        status="In Progress",
        request_data={"queue": "product", "total": total},
    )

    # The per-item update path resolves variant_ids via the cached SKU map.
    # Without a fresh map, every update sends variants without ``id`` and
    # Medusa rejects them with a "variant with sku X already exists" 400.
    # Build it once under a shared session for the whole batch.
    with optional_session() as (session, base_url):
        try:
            _build_medusa_sku_map(session, base_url)
        except Exception as e:
            update_medusa_log(
                log_name,
                status="Error",
                message=f"SKU map build failed before bulk sync: {e}",
            )
            raise

        for i in range(0, total, BATCH_SIZE):
            batch = queue[i:i + BATCH_SIZE]

            for item_code in batch:
                try:
                    MedusaProductExporter(item_code).export()
                    processed += 1
                except Exception as e:
                    errors += 1
                    frappe.log_error(f"Medusa bulk sync failed: {item_code}", str(e))

            _remove_from_queue(batch, QUEUE_KEY)
            frappe.db.commit()

            update_medusa_log(
                log_name,
                message=f"{processed}/{total} products synced, {errors} errors",
            )

            if i + BATCH_SIZE < total:
                time.sleep(BATCH_DELAY)

    update_medusa_log(
        log_name,
        status="Error" if errors else "Success",
        message=f"{processed}/{total} products synced, {errors} errors",
    )
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

    log_name = create_medusa_log(
        request_type="bulk_sync.process_queue",
        status="In Progress",
        request_data={"queue": "price", "total": total},
    )

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

        update_medusa_log(
            log_name,
            message=f"{processed}/{total} prices synced, {errors} errors",
        )

        if i + BATCH_SIZE < total:
            time.sleep(BATCH_DELAY)

    update_medusa_log(
        log_name,
        status="Error" if errors else "Success",
        message=f"{processed}/{total} prices synced, {errors} errors",
    )
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


# ── Product Sync scope gate ───────────────────────────────────


def _item_in_any_active_sync(doc) -> bool:
    """Delegate to the shared two-stage scope gate. Same wrapper
    pattern as ``shopware6/services/queue_hooks.py``."""
    from ecommerce_integrations.product_sync.constants import BACKEND_MEDUSA
    from ecommerce_integrations.product_sync.resolver import (
        item_is_in_any_active_sync,
    )
    return item_is_in_any_active_sync(doc, BACKEND_MEDUSA)
