"""
Shopware 6 Bulk Sync Dead Letter Queue (DLQ).

When a batch upload fails, the offending item codes used to be evicted from
the live queue and forgotten. They now land here. Entries carry an attempt
counter, the last error string and a timestamp. ``retry_failed_items_from_dlq``
(scheduler, hourly) re-enqueues entries with ``attempts < MAX_ATTEMPTS`` after
an exponential cool-off. Chronic failures (``attempts >= MAX_ATTEMPTS``) are
surfaced to ``Ecommerce Integration Log`` once and left in the DLQ for
manual inspection.

Storage layout (Redis hash, one per sync type, no Frappe site prefix because
``bulk_sync.py`` uses raw cache for the live queue too)::

    shopware6:sync_queue:{sync_type}:failed
        item_code -> {"attempts": int, "last_error": str, "last_attempt_ts": float}
"""

import json
import time
from typing import Any

import frappe

from ecommerce_integrations.shopware6.utils import get_logger

DLQ_KEY_PREFIX = "shopware6:sync_queue"
# Cap retries so a permanently broken item stops being re-enqueued forever.
MAX_ATTEMPTS = 3
# Backoff between retries grows with each failed attempt.
BACKOFF_BASE_SECONDS = 300  # 5 minutes for attempt 1 -> 5, 20, 80 minutes
BACKOFF_FACTOR = 4.0

logger = get_logger("bulk_sync_dlq")


def _dlq_key(sync_type: str) -> str:
    return f"{DLQ_KEY_PREFIX}:{sync_type}:failed"


def _cache():
    return frappe.cache()


def push_to_dlq(item_codes: list[str], sync_type: str, error: str = "") -> None:
    """Move failed items into the DLQ, incrementing per-item attempt counters."""
    if not item_codes:
        return

    cache = _cache()
    key = _dlq_key(sync_type)
    now = time.time()

    # Read current hash, mutate, write back. Frappe's cache wrapper does not
    # expose Redis HSET, so we serialize the whole hash as a JSON string.
    raw = cache.get_value(key)
    bucket: dict[str, dict[str, Any]] = {}
    if raw:
        try:
            bucket = json.loads(raw) if isinstance(raw, str) else raw
        except (ValueError, TypeError):
            bucket = {}

    for code in item_codes:
        existing = bucket.get(code) or {}
        attempts = int(existing.get("attempts", 0)) + 1
        bucket[code] = {
            "attempts": attempts,
            "last_error": (error or "")[:500],
            "last_attempt_ts": now,
        }

    cache.set_value(key, json.dumps(bucket), expires_in_sec=7 * 24 * 3600)

    logger.warning(
        f"Shopware6 DLQ: pushed {len(item_codes)} item(s) to {sync_type} DLQ "
        f"(total: {len(bucket)}). Reason: {error[:80] if error else 'unknown'}"
    )


def pop_from_dlq(sync_type: str, limit: int = 50) -> list[str]:
    """Pop up to ``limit`` items eligible for retry from the DLQ.

    An item is eligible when ``attempts < MAX_ATTEMPTS`` AND the exponential
    cool-off since ``last_attempt_ts`` has elapsed.
    """
    cache = _cache()
    key = _dlq_key(sync_type)
    raw = cache.get_value(key)
    if not raw:
        return []
    try:
        bucket = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        return []

    now = time.time()
    eligible: list[str] = []
    for code, meta in bucket.items():
        attempts = int(meta.get("attempts", 0))
        if attempts >= MAX_ATTEMPTS:
            continue
        last_ts = float(meta.get("last_attempt_ts", 0))
        cool_off = BACKOFF_BASE_SECONDS * (BACKOFF_FACTOR ** max(0, attempts - 1))
        if now - last_ts < cool_off:
            continue
        eligible.append(code)
        if len(eligible) >= limit:
            break

    # Remove popped items from the DLQ. They will land back in it if the
    # retry fails again (with attempts+1).
    if eligible:
        for code in eligible:
            bucket.pop(code, None)
        if bucket:
            cache.set_value(key, json.dumps(bucket), expires_in_sec=7 * 24 * 3600)
        else:
            cache.delete_value(key)

    return eligible


def get_dlq_status(sync_type: str) -> dict[str, Any]:
    """Read-only status for monitoring/admin UI."""
    cache = _cache()
    key = _dlq_key(sync_type)
    raw = cache.get_value(key)
    if not raw:
        return {"sync_type": sync_type, "size": 0, "chronic": 0, "items": []}
    try:
        bucket = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        return {"sync_type": sync_type, "size": 0, "chronic": 0, "items": []}

    items = [
        {
            "item_code": code,
            "attempts": int(meta.get("attempts", 0)),
            "last_error": meta.get("last_error", ""),
            "last_attempt_ts": meta.get("last_attempt_ts", 0),
        }
        for code, meta in bucket.items()
    ]
    chronic = sum(1 for it in items if it["attempts"] >= MAX_ATTEMPTS)
    return {
        "sync_type": sync_type,
        "size": len(items),
        "chronic": chronic,
        "items": items,
    }


def clear_dlq(sync_type: str) -> int:
    """Delete the whole DLQ for ``sync_type``. Returns the number of entries removed."""
    cache = _cache()
    key = _dlq_key(sync_type)
    raw = cache.get_value(key)
    if not raw:
        return 0
    try:
        bucket = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        bucket = {}
    cache.delete_value(key)
    return len(bucket)


def _surface_chronic_failures(sync_type: str) -> None:
    """Write a single Ecommerce Integration Log entry per chronic item.

    "Chronic" = attempts already at or above ``MAX_ATTEMPTS``. The dedup happens
    on the caller side (we only log once per scheduler tick, not per attempt).
    """
    from ecommerce_integrations.shopware6.utils import create_shopware_log

    status = get_dlq_status(sync_type)
    chronic_items = [it for it in status["items"] if it["attempts"] >= MAX_ATTEMPTS]
    if not chronic_items:
        return

    summary = "\n".join(
        f"{it['item_code']}: attempts={it['attempts']}, last_error={it['last_error'][:120]}"
        for it in chronic_items[:25]
    )
    try:
        create_shopware_log(
            method="bulk_sync_dlq.retry_failed_items_from_dlq",
            status="Error",
            message=(
                f"Shopware6 DLQ chronic failures ({sync_type}): "
                f"{len(chronic_items)} item(s) reached attempts>={MAX_ATTEMPTS}. "
                f"Manual review required.\n\n{summary}"
            ),
        )
    except Exception as e:
        logger.error(f"Failed to surface chronic DLQ failures: {e}", persist=False)


def retry_failed_items_from_dlq() -> dict[str, int]:
    """Scheduler entry point: move retry-eligible DLQ items back to the live queue.

    Registered as an hourly scheduler task in ``hooks.py``.
    """
    from ecommerce_integrations.shopware6.bulk_sync import (
        add_to_sync_queue,
        schedule_bulk_sync_processing,
    )

    results: dict[str, int] = {}
    for sync_type in ("product", "properties", "price"):
        eligible = pop_from_dlq(sync_type, limit=50)
        for code in eligible:
            add_to_sync_queue(code, sync_type)
        if eligible:
            schedule_bulk_sync_processing()
        results[sync_type] = len(eligible)

        # Surface chronic failures (items that aren't eligible because they
        # already exceeded MAX_ATTEMPTS) — one log per scheduler tick.
        _surface_chronic_failures(sync_type)

    if any(results.values()):
        logger.info(f"Shopware6 DLQ retry: re-enqueued {results}")
    return results
