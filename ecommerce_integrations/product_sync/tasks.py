"""Orchestrator for the Ecommerce Product Sync feature.

``apply_sync`` is the single entry point — dry-run delegates to the
differ; a live run pushes the diff through the registered adapter,
persists an ``Ecommerce Sync Run`` audit row, writes
``Ecommerce Sync Error`` rows on failure, and updates
``tabEcommerce Item.last_synced_hash`` per successful push.

The apply loop is serial per item. The Shopware adapter already has
a private ``_bulk_upsert`` helper that chunks at 100/request; wiring
it through the orchestrator is a future enhancement (when single-call
latency starts mattering relative to the per-item canonical-payload
build time).
"""

from __future__ import annotations

import time
import traceback
from typing import Any

import frappe
from frappe import _
from frappe.utils import now_datetime

from ecommerce_integrations.product_sync.constants import (
    ACTION_CREATE,
    ACTION_UPDATE,
    BACKEND_MEDUSA,
    BACKEND_SHOPWARE,
    BACKEND_TO_INTEGRATION_KEY,
    OVERRIDE_DOCTYPE,
    STATUS_ERROR,
    STATUS_OK,
    STATUS_PARTIAL,
    STATUS_RUNNING,
    SYNC_DOCTYPE,
)
from ecommerce_integrations.product_sync.differ import compute_product_diff
from ecommerce_integrations.product_sync.engine.canonical import (
    build_canonical_payload,
    compute_hash,
)
from ecommerce_integrations.product_sync.engine.preview import (
    ProductSyncPreviewPlan,
    ProductSyncRunResult,
)


_ECOMMERCE_ITEM_DOCTYPE = "Ecommerce Item"
_RUN_DOCTYPE = "Ecommerce Sync Run"
_ERROR_DOCTYPE = "Ecommerce Sync Error"

# How often (in items) to publish a realtime progress event.
_PROGRESS_EVERY_N_ITEMS = 5

# How often (in items) to check the cancel_requested flag on the Run.
_CANCEL_CHECK_EVERY_N_ITEMS = 25


def apply_sync(
    sync_name: str,
    *,
    dry_run: bool = True,
    max_items: int | None = None,
    fetch_live: bool = True,
    subset_item_codes: list[str] | None = None,
    mode: str = "live",
    triggered_by: str | None = None,
    trigger_type: str = "manual",
) -> ProductSyncPreviewPlan | ProductSyncRunResult:
    """Run one Product Sync end-to-end.

    Args:
        sync_name: Document name of the ``Ecommerce Product Sync``.
        dry_run: When True (default), returns a preview plan without
            touching the backend. When False, pushes the diff and
            persists a Sync Run row.
        max_items: Preview-only cap. None = unlimited.
        fetch_live: When True, the differ calls the backend adapter
            for per-field diffs + orphan/drift detection. Pass False
            for hash-only fast mode.
        subset_item_codes: Optional pre-filtered list of item_codes —
            applies to both dry-run and live runs.
        mode: One of ``live`` / ``dryrun`` / ``preflight`` / ``sandbox``
            / ``subset`` / ``rollback`` — persisted on the Run doc for
            audit. Only ``live`` defaults to ``dry_run=False``.
        triggered_by: User who started the run (Phase-5 sets it from
            ``frappe.session.user``). Persisted on Run for audit.
        trigger_type: ``manual`` / ``cron`` / ``webhook`` / ``dispatch``.
    """
    doc = frappe.get_doc(SYNC_DOCTYPE, sync_name)
    if dry_run:
        return compute_product_diff(
            doc,
            max_items=max_items,
            fetch_live=fetch_live,
            subset_item_codes=subset_item_codes,
        )
    return _apply_live(
        doc,
        subset_item_codes=subset_item_codes,
        mode=mode,
        triggered_by=triggered_by or frappe.session.user,
        trigger_type=trigger_type,
    )


def _apply_live(
    sync_doc,
    *,
    subset_item_codes: list[str] | None = None,
    mode: str = "live",
    triggered_by: str | None = None,
    trigger_type: str = "manual",
) -> ProductSyncRunResult:
    """Real apply path. Pushes the diff through the adapter, persists
    a Sync Run row, writes Sync Error rows on per-item failure,
    updates ``tabEcommerce Item.last_synced_hash`` on success.
    """
    backend = sync_doc.backend or ""
    integration_key = BACKEND_TO_INTEGRATION_KEY.get(backend, backend.lower())
    started_at = now_datetime()
    started_ts = time.time()

    # Claim the sync — refuse a second concurrent run on the same doc.
    if not _claim_sync(sync_doc.name):
        return ProductSyncRunResult(
            sync=sync_doc.name,
            backend=backend,
            status=STATUS_PARTIAL,
            message=_("Sync ist bereits aktiv — paralleler Run abgelehnt."),
        )

    # Open the Run doc immediately so the UI can poll it.
    run = _create_run_doc(
        sync_doc, mode=mode, triggered_by=triggered_by,
        trigger_type=trigger_type, started_at=started_at,
    )

    result = ProductSyncRunResult(
        sync=sync_doc.name,
        backend=backend,
        status=STATUS_RUNNING,
    )

    applied_diffs: list[dict] = []
    plan = None
    try:
        # 1. Build the diff (full set, no max_items cap for apply).
        plan = compute_product_diff(
            sync_doc,
            max_items=None,
            fetch_live=True,
            subset_item_codes=subset_item_codes,
        )
        # Persist the plan up-front so the operator can see what was
        # intended even if the apply loop crashes halfway. Use
        # db.set_value to avoid the check_if_latest race with later
        # _finalize_run writes (same row, same writer).
        frappe.db.set_value(
            _RUN_DOCTYPE, run.name,
            {
                "preview_plan_json": frappe.as_json(plan.to_dict(), indent=1),
                "items_total": len(plan.creates) + len(plan.updates),
            },
            update_modified=False,
        )

        # 2. Get the adapter (lazy import to keep tasks importable
        # even on sites without the backend installed).
        from ecommerce_integrations.product_sync.engine.adapters.base import AdapterError
        from ecommerce_integrations.product_sync.engine.registry import get_adapter

        try:
            adapter = get_adapter(backend)
        except AdapterError as exc:
            _finalize_run(
                run, result, status=STATUS_ERROR,
                message=str(exc), applied_diffs=[], started_ts=started_ts,
            )
            _release_sync(sync_doc.name, status=STATUS_ERROR, last_error=str(exc))
            return result

        target_channels = [
            row.sales_channel_id
            for row in (sync_doc.target_sales_channels or [])
            if row.sales_channel_id
        ]

        # 3. Apply creates + updates serially. Phase-5.1 will batch.
        to_process = []
        for node in plan.creates:
            to_process.append((node, ACTION_CREATE))
        for node in plan.updates:
            to_process.append((node, ACTION_UPDATE))

        for i, (node, action) in enumerate(to_process):
            # Heartbeat + cancel check every N items.
            if i and i % _CANCEL_CHECK_EVERY_N_ITEMS == 0:
                if _is_cancelled(run.name):
                    result.message = _("Run wurde durch den Operator abgebrochen.")
                    _finalize_run(
                        run, result, status=STATUS_PARTIAL,
                        message=result.message, applied_diffs=applied_diffs,
                        started_ts=started_ts,
                    )
                    _release_sync(
                        sync_doc.name, status=STATUS_PARTIAL,
                        last_error=result.message,
                    )
                    return result
                _heartbeat(run.name)

            # Realtime progress.
            if i and i % _PROGRESS_EVERY_N_ITEMS == 0:
                _publish_progress(sync_doc.name, run.name, i, len(to_process), result)

            try:
                outcome = _apply_one_item(
                    item_code=node.item_code,
                    action=action,
                    sync_doc=sync_doc,
                    adapter=adapter,
                    target_channels=target_channels,
                    integration_key=integration_key,
                    run_name=run.name,
                )
                applied_diffs.append(outcome)
                if action == ACTION_CREATE:
                    result.created += 1
                else:
                    result.updated += 1
            except Exception as exc:  # noqa: BLE001
                _record_item_failure(
                    sync_doc.name, node.item_code, backend, exc,
                    payload=None, response=None, run_name=run.name,
                )
                result.errors.append(f"{node.item_code}: {exc}")
                applied_diffs.append({
                    "item_code": node.item_code,
                    "action": action,
                    "status": "error",
                    "error": str(exc),
                })

        # 4. Orphan handling per orphan_policy.
        orphan_policy = (sync_doc.orphan_policy or "keep").lower()
        if orphan_policy in ("deactivate", "delete") and plan.orphans:
            for o in plan.orphans:
                try:
                    if orphan_policy == "deactivate":
                        adapter.deactivate_product(o.external_id)
                        result.deactivated += 1
                    else:
                        adapter.delete_product(o.external_id)
                        result.deleted += 1
                except Exception as exc:  # noqa: BLE001
                    result.errors.append(f"orphan {o.external_id}: {exc}")

        # 5. Mapping-drift cleanup.
        for drift in plan.mapping_drift:
            # Clear the stale external_id from tabEcommerce Item; the
            # next run will treat the item as a fresh create.
            _clear_mapping(drift.item_code, integration_key)

        # 6. Final status.
        status = STATUS_OK if not result.errors else STATUS_PARTIAL
        _finalize_run(
            run, result, status=status,
            message=None, applied_diffs=applied_diffs,
            started_ts=started_ts,
        )
        _release_sync(
            sync_doc.name, status=status,
            last_error="\n".join(result.errors[:5]) if result.errors else None,
        )
        result.status = status
        return result

    except Exception as exc:  # noqa: BLE001
        # Top-level catastrophic failure — bail with full traceback.
        tb = traceback.format_exc()
        msg = f"unexpected error: {type(exc).__name__}: {exc}"
        _finalize_run(
            run, result, status=STATUS_ERROR,
            message=msg, applied_diffs=applied_diffs, started_ts=started_ts,
        )
        _release_sync(sync_doc.name, status=STATUS_ERROR, last_error=msg)
        frappe.log_error(title=f"Product Sync apply crashed: {sync_doc.name}", message=tb)
        result.status = STATUS_ERROR
        result.message = msg
        return result


# ─── Per-item application ────────────────────────────────────────────


def _apply_one_item(
    *,
    item_code: str,
    action: str,
    sync_doc,
    adapter,
    target_channels: list[str],
    integration_key: str,
    run_name: str,
) -> dict:
    """Push one item to the backend, persist mapping + hash on success."""
    from ecommerce_integrations.product_sync.engine.payload import (
        build_medusa_payload,
        build_shopware_payload,
    )

    item = frappe.get_doc("Item", item_code)
    canonical = build_canonical_payload(item, sync_doc)
    proposed_hash = compute_hash(canonical)

    # Load existing mapping for update path.
    ecom_row = frappe.db.get_value(
        _ECOMMERCE_ITEM_DOCTYPE,
        {"erpnext_item_code": item_code, "integration": integration_key},
        ["name", "integration_item_code"],
        as_dict=True,
    )
    external_id = ecom_row.get("integration_item_code") if ecom_row else None

    if sync_doc.backend == BACKEND_SHOPWARE:
        payload = build_shopware_payload(item, sync_doc, canonical, external_id=external_id)
    elif sync_doc.backend == BACKEND_MEDUSA:
        payload = build_medusa_payload(item, sync_doc, canonical, external_id=external_id)
    else:
        raise ValueError(f"Unknown backend: {sync_doc.backend}")

    new_external_id = adapter.upsert_product(
        external_id=external_id,
        payload=payload,
        target_sales_channels=target_channels,
    )

    _persist_item_success(
        item_code=item_code,
        integration=integration_key,
        external_id=new_external_id,
        proposed_hash=proposed_hash,
        run_name=run_name,
        existing_row=ecom_row,
    )

    return {
        "item_code": item_code,
        "action": action,
        "status": "ok",
        "external_id": new_external_id,
    }


def _persist_item_success(
    *,
    item_code: str,
    integration: str,
    external_id: str,
    proposed_hash: str,
    run_name: str,
    existing_row: dict | None,
) -> None:
    """Upsert tabEcommerce Item with the new external_id + hash."""
    now = now_datetime()
    if existing_row:
        frappe.db.set_value(
            _ECOMMERCE_ITEM_DOCTYPE,
            existing_row["name"],
            {
                "integration_item_code": external_id,
                "last_synced_hash": proposed_hash,
                "last_synced_at": now,
                "last_sync_run": run_name,
            },
            update_modified=False,
        )
    else:
        doc = frappe.get_doc({
            "doctype": _ECOMMERCE_ITEM_DOCTYPE,
            "erpnext_item_code": item_code,
            "integration": integration,
            "integration_item_code": external_id,
            "sku": item_code,
            "last_synced_hash": proposed_hash,
            "last_synced_at": now,
            "last_sync_run": run_name,
        })
        doc.flags.ignore_permissions = True
        doc.insert()


def _clear_mapping(item_code: str, integration: str) -> None:
    """Remove the integration_item_code so the next run creates fresh.

    Called for ``mapping_drift`` items where the recorded backend id
    no longer exists in the live tree.
    """
    name = frappe.db.get_value(
        _ECOMMERCE_ITEM_DOCTYPE,
        {"erpnext_item_code": item_code, "integration": integration},
        "name",
    )
    if not name:
        return
    frappe.db.set_value(
        _ECOMMERCE_ITEM_DOCTYPE, name,
        {
            "integration_item_code": "",
            "last_synced_hash": "",
        },
        update_modified=False,
    )


# ─── Run lifecycle ───────────────────────────────────────────────────


def _create_run_doc(
    sync_doc, *, mode: str, triggered_by: str | None,
    trigger_type: str, started_at,
):
    """Persist a fresh Sync Run doc and return it."""
    primary = ""
    for row in (sync_doc.target_sales_channels or []):
        if int(row.is_primary or 0):
            primary = row.sales_channel_id or ""
            break
    is_sandbox = mode == "sandbox"
    doc = frappe.get_doc({
        "doctype": _RUN_DOCTYPE,
        "sync": sync_doc.name,
        "backend": sync_doc.backend or "",
        "status": STATUS_RUNNING,
        "mode": mode,
        "trigger_type": trigger_type,
        "triggered_by": triggered_by or frappe.session.user,
        "started_at": started_at,
        "target_channel_id": primary,
        "is_sandbox": 1 if is_sandbox else 0,
    })
    doc.flags.ignore_permissions = True
    doc.insert()
    return doc


def _finalize_run(
    run, result: ProductSyncRunResult, *,
    status: str, message: str | None,
    applied_diffs: list[dict],
    started_ts: float,
) -> None:
    """Write final stats + applied_diffs onto the Run doc.

    Uses ``db.set_value`` instead of ``doc.save()`` to bypass
    ``check_if_latest``: the apply loop may have updated the row via
    ``preview_plan_json``-save earlier, so the in-memory ``run`` doc's
    timestamp is stale. set_value writes raw and skips the freshness
    check, which is what we want here — only one writer exists for
    this row (the apply loop owns it for its whole lifetime).
    """
    finished = now_datetime()
    duration_ms = int((time.time() - started_ts) * 1000)
    frappe.db.set_value(
        _RUN_DOCTYPE, run.name,
        {
            "status": status,
            "finished_at": finished,
            "duration_ms": duration_ms,
            "items_succeeded": result.created + result.updated,
            "items_failed": len(result.errors),
            "items_skipped": result.skipped,
            "created_count": result.created,
            "updated_count": result.updated,
            "deactivated_count": result.deactivated,
            "deleted_count": result.deleted,
            "error_summary": "\n".join(result.errors[:10]) if result.errors else "",
            "applied_diffs_json": frappe.as_json(applied_diffs, indent=1),
        },
        update_modified=True,
    )
    frappe.db.commit()  # noqa: SLF001 — persist Run before returning


def _record_item_failure(
    sync_name: str, item_code: str, backend: str, exc: Exception,
    *, payload: Any, response: Any, run_name: str,
) -> None:
    """Write an Ecommerce Sync Error row with severity classification.

    Severity rules:
    - HTTP 4xx with validation message  → ``immediate`` (no auto-retry)
    - HTTP 5xx / timeout / network      → ``transient`` (retry path)
    - Business conflict (SKU taken)     → ``manual_review``
    - Anything else                     → ``immediate`` (safe default)
    """
    severity = _classify_error(exc)
    msg = str(exc)[:1000]
    doc = frappe.get_doc({
        "doctype": _ERROR_DOCTYPE,
        "sync": sync_name,
        "item_code": item_code,
        "backend": backend,
        "severity": severity,
        "attempt_count": 1,
        "last_attempt_at": now_datetime(),
        "last_error_message": msg,
        "last_error_payload": frappe.as_json(payload, indent=1) if payload else "",
        "last_response_payload": frappe.as_json(response, indent=1) if response else "",
        "sync_run": run_name,
        "resolved": 0,
    })
    doc.flags.ignore_permissions = True
    doc.insert()


def _classify_error(exc: Exception) -> str:
    s = str(exc).lower()
    if any(t in s for t in ("timeout", "connection", "5xx", "503", "502", "504")):
        return "transient"
    if any(t in s for t in ("duplicate", "already exists", "sku is taken", "conflict")):
        return "manual_review"
    return "immediate"


# ─── Sync-level claim / release / heartbeat ──────────────────────────
# Thin wrappers over ``product_sync._locks`` so push and pull share the
# same atomicity contract.


def _claim_sync(sync_name: str) -> bool:
    from ecommerce_integrations.product_sync._locks import claim_sync
    return claim_sync(SYNC_DOCTYPE, sync_name)


def _release_sync(sync_name: str, *, status: str, last_error: str | None) -> None:
    from ecommerce_integrations.product_sync._locks import release_sync
    release_sync(SYNC_DOCTYPE, sync_name, status=status, last_error=last_error)


def _heartbeat(run_name: str) -> None:
    from ecommerce_integrations.product_sync._locks import heartbeat_run
    heartbeat_run(_RUN_DOCTYPE, run_name)


def _is_cancelled(run_name: str) -> bool:
    return bool(frappe.db.get_value(_RUN_DOCTYPE, run_name, "cancel_requested"))


def _publish_progress(
    sync_name: str, run_name: str, current: int, total: int,
    result: ProductSyncRunResult,
) -> None:
    """Realtime broadcast scoped to the Sync doc — only users with
    read on the doc receive it (frappe.realtime semantics)."""
    try:
        frappe.publish_realtime(
            event=f"product_sync:progress:{sync_name}",
            message={
                "run": run_name,
                "current": current,
                "total": total,
                "created": result.created,
                "updated": result.updated,
                "errors": len(result.errors),
            },
            doctype=SYNC_DOCTYPE,
            docname=sync_name,
        )
    except Exception:
        # Realtime is best-effort — never crash apply on broadcast.
        pass


# ─── Scheduler entry ─────────────────────────────────────────────────


def dispatch_due_syncs() -> dict:
    """Scheduler entry — delegates to the dispatcher module.

    Wired in hooks.py to fire every 15 minutes. The actual logic
    lives in ``product_sync.scheduler`` so the dispatch decisions
    are testable in isolation.
    """
    from ecommerce_integrations.product_sync.scheduler import dispatch_due_syncs as _do
    return _do()
