"""Pull-side orchestrator (Backend → ERPNext).

Inverse of :mod:`tasks` which handles the push direction. ``apply_pull``
delegates per-target work to the existing per-backend pull helpers
(shopware6 order sync, medusa order/customer sync), persisting an
``Ecommerce Sync Run`` row with ``mode="pull"`` so both directions
share a single audit trail.

Phase-8 first cut wires the orchestration scaffolding and the watermark
plumbing; the actual fetch handlers either reuse the existing scheduled
``shopware6/medusa`` pull functions or — where those don't exist yet —
add a minimal incremental fetch via the backend's
``updated_at`` / ``modified_at`` filter.
"""

from __future__ import annotations

import time
import traceback

import frappe
from frappe import _
from frappe.utils import now_datetime

from ecommerce_integrations.product_sync.constants import (
    STATUS_ERROR,
    STATUS_OK,
    STATUS_PARTIAL,
    STATUS_RUNNING,
)


_PULL_SYNC_DOCTYPE = "Ecommerce Pull Sync"
_RUN_DOCTYPE = "Ecommerce Sync Run"


def apply_pull(
    sync_name: str,
    *,
    trigger_type: str = "manual",
) -> dict:
    """Run one Pull Sync end-to-end.

    Phase-8: returns a dict result (no dataclass needed since the
    push-side ``ProductSyncRunResult`` already covers most fields and
    the Pull-side reuses the same Sync Run doctype). The actual
    per-target handlers are pluggable: each target is dispatched to a
    backend-specific helper that may be a stub today (Phase-8.1 wires
    real fetches).
    """
    doc = frappe.get_doc(_PULL_SYNC_DOCTYPE, sync_name)
    if not int(doc.is_active or 0):
        return {"status": "skipped", "reason": "pull sync inactive"}
    if not _claim_pull(sync_name):
        return {"status": "partial", "reason": "another pull is already running"}

    started_at = now_datetime()
    started_ts = time.time()

    run = _create_pull_run(doc, trigger_type=trigger_type, started_at=started_at)

    counts = {
        "orders_pulled": 0, "orders_failed": 0,
        "customers_pulled": 0, "customers_failed": 0,
        "inventory_pulled": 0, "inventory_failed": 0,
    }
    errors: list[str] = []
    try:
        if int(doc.pull_orders or 0):
            _pull_orders(doc, run, counts, errors)
        if int(doc.pull_customers or 0):
            _pull_customers(doc, run, counts, errors)
        if int(doc.pull_inventory or 0):
            _pull_inventory(doc, run, counts, errors)

        status = STATUS_OK if not errors else STATUS_PARTIAL
        _finalize_pull_run(run, counts, errors, started_ts, status)
        _release_pull(sync_name, status=status, last_error="\n".join(errors[:5]))
        return {
            "status": status,
            "run": run.name,
            "counts": counts,
            "errors": errors[:10],
        }
    except Exception as exc:  # noqa: BLE001
        tb = traceback.format_exc()
        _finalize_pull_run(run, counts, [str(exc)], started_ts, STATUS_ERROR)
        _release_pull(sync_name, status=STATUS_ERROR, last_error=str(exc)[:500])
        frappe.log_error(title=f"Pull Sync crashed: {sync_name}", message=tb)
        return {"status": STATUS_ERROR, "message": str(exc)}


# ─── Per-target dispatchers ──────────────────────────────────────────
# The three pull targets (orders / customers / inventory) share the
# same dispatch shape: pick the backend module, call its handler, update
# the matching watermark column. ``_PULL_TARGETS`` captures the only
# bits that vary; ``_pull_one_target`` is the single executor.

_PULL_TARGETS: dict[str, dict[str, str]] = {
    "orders": {
        "handler": "pull_orders",
        "ok_key": "orders_pulled",
        "err_key": "orders_failed",
        "watermark": "last_order_pulled_at",
        "label": "Order",
    },
    "customers": {
        "handler": "pull_customers",
        "ok_key": "customers_pulled",
        "err_key": "customers_failed",
        "watermark": "last_customer_pulled_at",
        "label": "Customer",
    },
    "inventory": {
        "handler": "pull_inventory",
        "ok_key": "inventory_pulled",
        "err_key": "inventory_failed",
        "watermark": "last_inventory_pulled_at",
        "label": "Inventory",
    },
}


def _backend_handler_module(backend: str):
    """Lazy-load the pull-handler module for ``backend``.

    Returns ``None`` for unsupported backends so the caller can record
    a clear error instead of an ImportError trace.
    """
    if backend == "Shopware":
        from ecommerce_integrations.product_sync.pull_handlers import shopware
        return shopware
    if backend == "Medusa":
        from ecommerce_integrations.product_sync.pull_handlers import medusa
        return medusa
    return None


def _pull_one_target(
    doc, target: str, counts: dict, errors: list[str],
) -> None:
    """Run one pull target (orders / customers / inventory)."""
    cfg = _PULL_TARGETS[target]
    backend = (doc.backend or "").strip()
    handler_module = _backend_handler_module(backend)
    if handler_module is None:
        # Only orders' original implementation surfaced "Unknown
        # backend"; reproduce that behaviour rather than going quiet on
        # customers/inventory.
        if target == "orders":
            errors.append(f"Unknown backend for pull: {backend}")
        return

    try:
        n_ok, n_err = getattr(handler_module, cfg["handler"])(doc)
        counts[cfg["ok_key"]] = n_ok
        counts[cfg["err_key"]] = n_err
        frappe.db.set_value(
            _PULL_SYNC_DOCTYPE, doc.name,
            {cfg["watermark"]: now_datetime()},
            update_modified=False,
        )
    except NotImplementedError as exc:
        errors.append(
            f"{cfg['label']} pull not yet implemented for {backend}: {exc}",
        )
    except Exception as exc:  # noqa: BLE001
        errors.append(f"{cfg['label']} pull crashed: {exc}")


def _pull_orders(doc, run, counts: dict, errors: list[str]) -> None:
    _pull_one_target(doc, "orders", counts, errors)


def _pull_customers(doc, run, counts: dict, errors: list[str]) -> None:
    _pull_one_target(doc, "customers", counts, errors)


def _pull_inventory(doc, run, counts: dict, errors: list[str]) -> None:
    _pull_one_target(doc, "inventory", counts, errors)


# ─── Run lifecycle ───────────────────────────────────────────────────


def _create_pull_run(doc, *, trigger_type: str, started_at):
    """Persist a fresh Sync Run row for this pull.

    The ``Sync Run.sync`` field is a Link to ``Ecommerce Product Sync``
    by historic design. For Pull-mode runs the value stores the Pull
    Sync's name — same field, different doctype. We use
    ``flags.ignore_links`` so Frappe's link-existence check doesn't
    reject it. Phase-8.1 will convert ``sync`` to a Dynamic Link.
    """
    run = frappe.get_doc({
        "doctype": _RUN_DOCTYPE,
        "sync": doc.name,
        "backend": doc.backend or "",
        "status": STATUS_RUNNING,
        "mode": "pull",
        "trigger_type": trigger_type,
        "triggered_by": frappe.session.user,
        "started_at": started_at,
    })
    run.flags.ignore_permissions = True
    run.flags.ignore_links = True
    run.insert()
    return run


def _finalize_pull_run(
    run, counts: dict, errors: list[str], started_ts: float, status: str,
) -> None:
    finished = now_datetime()
    duration_ms = int((time.time() - started_ts) * 1000)
    total_ok = counts["orders_pulled"] + counts["customers_pulled"] + counts["inventory_pulled"]
    total_err = counts["orders_failed"] + counts["customers_failed"] + counts["inventory_failed"]
    frappe.db.set_value(
        _RUN_DOCTYPE, run.name,
        {
            "status": status,
            "finished_at": finished,
            "duration_ms": duration_ms,
            "items_total": total_ok + total_err,
            "items_succeeded": total_ok,
            "items_failed": total_err,
            "error_summary": "\n".join(errors[:10]) if errors else "",
            "applied_diffs_json": frappe.as_json(counts, indent=1),
        },
        update_modified=True,
    )
    frappe.db.commit()  # noqa: SLF001


def _claim_pull(sync_name: str) -> bool:
    from ecommerce_integrations.product_sync._locks import claim_sync
    return claim_sync(_PULL_SYNC_DOCTYPE, sync_name)


def _release_pull(sync_name: str, *, status: str, last_error: str | None) -> None:
    from ecommerce_integrations.product_sync._locks import release_sync
    release_sync(_PULL_SYNC_DOCTYPE, sync_name, status=status, last_error=last_error)


# ─── Scheduler dispatcher ────────────────────────────────────────────


def dispatch_due_pulls() -> dict:
    """Scheduler entry — runs every 15 minutes via hooks.py.

    Same cron-decision logic as the push-side dispatcher
    (:mod:`product_sync.scheduler`). Phase-8 first cut: simple
    interval check against ``last_synced_at``. Custom cron expressions
    are deferred to Phase-8.1 (croniter dep gate).
    """
    if frappe.flags.in_install or frappe.flags.in_migrate:
        return {"queued": 0, "skipped_install": True}
    if not frappe.db.exists("DocType", _PULL_SYNC_DOCTYPE):
        return {"queued": 0, "no_doctype": True}

    from ecommerce_integrations.product_sync.scheduler import _is_due

    rows = frappe.get_all(
        _PULL_SYNC_DOCTYPE,
        filters={"is_active": 1},
        fields=[
            "name", "backend", "cron_preset", "cron_schedule",
            "last_synced_at", "sync_status",
        ],
        limit=0,
    )
    now = now_datetime()
    queued: list[str] = []
    for row in rows:
        if (row.sync_status or "").lower() == "running":
            continue
        if not _is_due(row, now):
            continue
        try:
            frappe.enqueue(
                "ecommerce_integrations.product_sync.pull_tasks.apply_pull",
                queue="long",
                timeout=3600,
                is_async=True,
                job_name=f"product_sync:pull:{row.name}",
                sync_name=row.name,
                trigger_type="cron",
            )
            queued.append(row.name)
        except Exception:
            frappe.log_error(
                title=f"Pull Sync dispatch failed: {row.name}",
                message=frappe.get_traceback(),
            )
    return {"queued": len(queued), "names": queued}


# ─── Whitelisted UI endpoints (Setting-form widget) ──────────────────


@frappe.whitelist()
def list_pull_syncs_for_backend(backend: str) -> list[dict]:
    """All Pull Syncs for one backend — for the Setting-form widget.

    Mirrors ``product_sync.api.list_for_backend`` for the push side. The
    widget reads from here to render the per-backend Pull Sync table on
    the Shopware / Medusa Setting form.
    """
    if not frappe.has_permission(_PULL_SYNC_DOCTYPE, "read"):
        frappe.throw(_("Not permitted to read Pull Syncs"))
    if backend not in ("Shopware", "Medusa"):
        frappe.throw(_("Unknown backend: {0}").format(backend))
    return frappe.get_all(
        _PULL_SYNC_DOCTYPE,
        filters={"backend": backend},
        fields=[
            "name", "title", "is_active", "cron_preset",
            "sync_status", "last_synced_at",
            "items_pulled_total", "items_failed_total",
            "last_order_pulled_at", "last_customer_pulled_at",
            "last_inventory_pulled_at",
        ],
        order_by="modified desc",
    )


@frappe.whitelist()
def run_pull_now(sync: str) -> dict:
    """Trigger a manual pull. Wraps :func:`apply_pull` with permission
    enforcement so the Setting-form widget can offer a "Run Now" button
    without exposing the bare ``apply_pull`` entry point.
    """
    if not frappe.has_permission(_PULL_SYNC_DOCTYPE, "write", doc=sync):
        frappe.throw(_("Not permitted to run this Pull Sync"))
    return apply_pull(sync, trigger_type="manual")
