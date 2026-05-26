"""Per-sync cron dispatcher.

Frappe's ``scheduler_events`` is static (cron expressions defined in
hooks.py at app-install time). We need *per-document* schedules so
each ``Ecommerce Product Sync`` can have its own cadence. The pattern
to solve this is well-known: one central tick (every 15 minutes) reads
all active syncs, evaluates each one's ``cron_preset`` / ``cron_schedule``
against "when did this sync last run vs. now", and enqueues the due
ones into a background queue.

``croniter`` would be the precise way to evaluate cron expressions
but it's an external dep. We side-step it for Phase 6 by translating
the common presets to plain Python ``datetime`` comparisons and
deferring custom expressions to ``croniter`` if available — falling
back to a permissive "if more than an hour passed, run it" rule.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import frappe
from frappe.utils import get_datetime, now_datetime

from ecommerce_integrations.product_sync.constants import SYNC_DOCTYPE


# Preset → cadence in minutes.
_PRESET_INTERVAL_MINUTES = {
    "every_hour": 60,
    "every_6h": 360,
    "daily_3am": 60 * 24,
}


def dispatch_due_syncs() -> dict:
    """Scheduler entry — runs every 15 minutes via hooks.py.

    Walks all active Product Syncs, decides which are due, enqueues
    them in the long-running queue. Returns a small dict for
    observability.
    """
    if frappe.flags.in_install or frappe.flags.in_migrate:
        return {"queued": 0, "skipped_install": True}
    if not frappe.db.exists("DocType", SYNC_DOCTYPE):
        return {"queued": 0, "no_doctype": True}

    now = now_datetime()
    queued: list[str] = []
    inspected = 0

    rows = frappe.get_all(
        SYNC_DOCTYPE,
        filters={"is_active": 1},
        fields=[
            "name", "backend", "cron_preset", "cron_schedule",
            "last_synced_at", "sync_status",
        ],
        limit=0,
    )
    for row in rows:
        inspected += 1
        if (row.sync_status or "").lower() == "running":
            # A live run already owns this sync — don't double-dispatch.
            continue
        if not _is_due(row, now):
            continue
        try:
            _enqueue_sync(row.name)
            queued.append(row.name)
        except Exception:
            frappe.log_error(
                title=f"Product Sync dispatch failed: {row.name}",
                message=frappe.get_traceback(),
            )

    return {"queued": len(queued), "inspected": inspected, "names": queued}


def _is_due(row, now: datetime) -> bool:
    """True if this sync's schedule says it should run now."""
    preset = (row.cron_preset or "manual").strip()
    if preset == "manual":
        return False  # explicit "no cron"

    last = get_datetime(row.last_synced_at) if row.last_synced_at else None

    if preset in _PRESET_INTERVAL_MINUTES:
        interval = _PRESET_INTERVAL_MINUTES[preset]
        # daily_3am has a fixed time-of-day, not just an interval.
        if preset == "daily_3am":
            return _is_due_daily(last, now, hour=3)
        if last is None:
            return True
        return (now - last) >= timedelta(minutes=interval)

    if preset == "custom":
        expr = (row.cron_schedule or "").strip()
        if not expr:
            return False
        return _is_due_custom(expr, last, now)

    return False


def _is_due_daily(last: datetime | None, now: datetime, *, hour: int) -> bool:
    """Daily-at-HH:00 schedule: run when ``now`` has crossed today's
    target hour and the last run was before that target."""
    today_target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if now < today_target:
        return False
    if last is None:
        return True
    return last < today_target


def _is_due_custom(expr: str, last: datetime | None, now: datetime) -> bool:
    """Custom cron expression. Uses ``croniter`` if available; falls
    back to "more than an hour passed" — a safe lower bound for the
    typical operator-tuned expression (which is hourly or coarser).
    """
    try:
        from croniter import croniter
    except ImportError:
        if last is None:
            return True
        return (now - last) >= timedelta(hours=1)
    try:
        if last is None:
            # First-time run — fire if the expression has matched any
            # time in the last hour.
            base = now - timedelta(hours=1)
            it = croniter(expr, base)
            return it.get_next(datetime) <= now
        it = croniter(expr, last)
        return it.get_next(datetime) <= now
    except Exception:
        return False


def _enqueue_sync(sync_name: str) -> None:
    """Enqueue an apply_sync call into the long-running queue.

    Wrapped here so the call site is testable: the dispatcher just
    decides what to enqueue, the queue layer decides where.

    ``fetch_live=False``: the differ defaults to ``True`` for
    interactive previews (per-field drift detection needs the live
    response), but an unattended apply only needs the hash diff —
    drifted items get pushed regardless. With ``fetch_live=True``
    every drift candidate triggers a backend roundtrip, so a
    full-catalogue run after a canonical-schema bump (all hashes
    invalidated) blows past any RQ timeout before the apply phase
    starts.

    ``timeout`` is a generous backstop, not a hard cap. Worker hangs
    are reaped by the stale-run sweeper's heartbeat check, not by
    SIGKILL; setting it short just kills slow-but-progressing runs.
    """
    frappe.enqueue(
        "ecommerce_integrations.product_sync.tasks.apply_sync",
        queue="long",
        timeout=86400,
        is_async=True,
        job_name=f"product_sync:apply:{sync_name}",
        sync_name=sync_name,
        dry_run=False,
        fetch_live=False,
        mode="live",
        trigger_type="cron",
    )
