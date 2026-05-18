"""Whitelisted endpoints for the Product Sync UI.

Permission model (mirrors ``catalog_mirror.api``):

- Read-only endpoints (preview, preflight, list_test_history,
  product_match_by_sku) require ``read`` on the Sync doc.
- Mutating endpoints (apply_live, run_sandbox, run_subset,
  auto_adopt_by_sku, cancel_run, cleanup_sandbox, rollback_run) require
  ``write`` on the Sync doc.
- Listing across syncs (list_for_backend) requires ``read`` on the
  doctype, no per-doc filter.

All endpoints accept and return JSON-serialisable structures; the
``ProductSyncPreviewPlan.to_dict`` and related helpers handle the
dataclass-to-dict conversion.
"""

from __future__ import annotations

from collections.abc import Iterable

import frappe
from frappe import _

from ecommerce_integrations.product_sync.constants import (
    BACKEND_MEDUSA,
    BACKEND_SHOPWARE,
    BACKEND_TO_INTEGRATION_KEY,
    OVERRIDE_PIN,
    SYNC_DOCTYPE,
)

_OVERRIDE_DOCTYPE = "Ecommerce Product Sync Override"
_RUN_DOCTYPE = "Ecommerce Sync Run"
_ECOMMERCE_ITEM_DOCTYPE = "Ecommerce Item"


# ─── Preview / Pre-Flight ────────────────────────────────────────────


@frappe.whitelist()
def preview_sync(
    sync: str,
    *,
    max_items: int | None = None,
    fetch_live: bool = True,
    subset_item_codes: list[str] | None = None,
) -> dict:
    """Synchronous preview — returns the full plan in one round-trip.

    Suitable for small scopes (≲ a few thousand Items). For large
    catalogues use :func:`start_preview` / :func:`get_preview_status`
    so the proxy doesn't kill the request at 30/60 s.
    """
    if not frappe.has_permission(SYNC_DOCTYPE, "read", doc=sync):
        frappe.throw(_("Not permitted to read this Product Sync"))
    from ecommerce_integrations.product_sync.tasks import apply_sync

    if isinstance(subset_item_codes, str):
        subset_item_codes = [s.strip() for s in subset_item_codes.splitlines() if s.strip()]
    plan = apply_sync(
        sync,
        dry_run=True,
        max_items=_coerce_int(max_items),
        fetch_live=_coerce_bool(fetch_live, default=True),
        subset_item_codes=subset_item_codes,
    )
    return plan.to_dict() if hasattr(plan, "to_dict") else plan


@frappe.whitelist()
def start_preview(
    sync: str,
    *,
    max_items: int | None = None,
    fetch_live: bool = False,
    detect_orphans: bool = False,
    subset_item_codes: list[str] | None = None,
) -> dict:
    """Kick off a background preview run.

    Creates an ``Ecommerce Sync Run`` row with ``mode='preview'``,
    enqueues the differ on the short queue, returns ``{run_name}``.
    The caller polls :func:`get_preview_status` to track progress and
    pick up the final plan when ready.

    Three preview modes, in increasing wall-time:

    - **Fast** (defaults, ``fetch_live=False``, ``detect_orphans=False``):
      pure hash-delta, no backend roundtrip. Bucket counts only —
      missing per-field diffs and orphan list. Typical: ~30 s on 30k
      items.
    - **Detail compare** (``fetch_live=True``): also fetches live
      Shopware/Medusa state for items where stored_hash !=
      proposed_hash (drift candidates), so the UPDATE bucket shows
      real per-field diffs. Adds 5–20 s depending on drift size.
    - **Full** (``fetch_live=True`` + ``detect_orphans=True``):
      additionally walks the target channels for backend products
      not in any mapping. Can take several minutes on a five-figure
      catalogue.

    The default is Fast because it answers "what would change?"
    correctly for 99 % of operator workflows; the heavier modes are
    opt-in from the preview dialog.
    """
    if not frappe.has_permission(SYNC_DOCTYPE, "read", doc=sync):
        frappe.throw(_("Not permitted to read this Product Sync"))

    if isinstance(subset_item_codes, str):
        subset_item_codes = [
            s.strip() for s in subset_item_codes.splitlines() if s.strip()
        ]

    run = frappe.get_doc({
        "doctype": _RUN_DOCTYPE,
        "sync": sync,
        "backend": frappe.db.get_value(SYNC_DOCTYPE, sync, "backend") or "",
        "status": "running",
        "mode": "preview",
        "trigger_type": "manual",
        "triggered_by": frappe.session.user,
        "started_at": frappe.utils.now_datetime(),
    })
    run.flags.ignore_permissions = True
    run.insert()
    frappe.db.commit()  # noqa: SLF001 — make the row visible to the worker

    # ``long`` queue: previews on big catalogues can run for minutes
    # (especially Detail / Full modes with backend roundtrips). The
    # short queue's worker pool is sized for sub-30 s jobs.
    frappe.enqueue(
        "ecommerce_integrations.product_sync.api._run_preview_in_background",
        queue="long",
        timeout=1800,
        sync=sync,
        run_name=run.name,
        max_items=_coerce_int(max_items),
        fetch_live=_coerce_bool(fetch_live, default=False),
        detect_orphans=_coerce_bool(detect_orphans, default=False),
        subset_item_codes=subset_item_codes,
    )
    return {"run_name": run.name, "status": "queued"}


def _run_preview_in_background(
    sync: str,
    run_name: str,
    max_items: int | None,
    fetch_live: bool,
    subset_item_codes: list[str] | None,
    detect_orphans: bool = False,
) -> None:
    """Background worker for :func:`start_preview`.

    Writes ``items_total`` + ``items_succeeded`` (= items processed)
    onto the Run row so the JS poller can drive a percentage bar.
    Throttles progress writes to once per ~500 ms to keep the row
    write-rate manageable on large catalogues.
    """
    import time
    from ecommerce_integrations.product_sync.differ import compute_product_diff

    doc = frappe.get_doc(SYNC_DOCTYPE, sync)
    last_write = [0.0]
    last_total = [0]

    def _on_progress(current: int, total: int) -> None:
        # First call: write the total once so the UI knows the
        # denominator straight away.
        if last_total[0] != total:
            frappe.db.set_value(
                _RUN_DOCTYPE, run_name,
                {"items_total": total},
                update_modified=False,
            )
            frappe.db.commit()  # noqa: SLF001
            last_total[0] = total
        now = time.time()
        # Throttle: only write every 500 ms or on the final tick.
        if (now - last_write[0]) < 0.5 and current < total:
            return
        last_write[0] = now
        frappe.db.set_value(
            _RUN_DOCTYPE, run_name,
            {"items_succeeded": current},
            update_modified=False,
        )
        frappe.db.commit()  # noqa: SLF001

    try:
        plan = compute_product_diff(
            doc,
            max_items=max_items,
            fetch_live=fetch_live,
            subset_item_codes=subset_item_codes,
            on_progress=_on_progress,
            detect_orphans=detect_orphans,
        )
        plan_dict = plan.to_dict() if hasattr(plan, "to_dict") else plan
        # Detail / Full previews can run for minutes during which the
        # MySQL connection idles past ``wait_timeout``. The retry
        # wrapper force-reconnects on (2006/2013) before re-running
        # the set_value so we don't lose the result at the finish line.
        _set_value_with_db_retry(
            _RUN_DOCTYPE, run_name,
            {
                "status": "ok",
                "finished_at": frappe.utils.now_datetime(),
                "preview_plan_json": frappe.as_json(plan_dict, indent=1),
                "items_succeeded": plan.items_in_scope,
                "items_total": plan.items_in_scope,
            },
            update_modified=True,
        )
        frappe.db.commit()  # noqa: SLF001
    except Exception as exc:  # noqa: BLE001
        import traceback
        try:
            frappe.db.rollback()
        except Exception:  # noqa: BLE001
            _force_db_reconnect()
        _set_value_with_db_retry(
            _RUN_DOCTYPE, run_name,
            {
                "status": "error",
                "finished_at": frappe.utils.now_datetime(),
                "error_summary": str(exc)[:500],
            },
            update_modified=True,
        )
        frappe.db.commit()  # noqa: SLF001
        frappe.log_error(
            title=f"Product Sync preview failed: {sync}",
            message=traceback.format_exc(),
        )


@frappe.whitelist()
def get_preview_status(run_name: str) -> dict:
    """Poll endpoint for a background preview.

    Returns ``{status, current, total, percent, plan?, error?}``.
    ``plan`` is only included when ``status == 'ok'``; ``error`` only
    when ``status == 'error'``. While running, callers should poll
    every 1–2 s.
    """
    if not frappe.db.exists(_RUN_DOCTYPE, run_name):
        return {"status": "missing"}

    row = frappe.db.get_value(
        _RUN_DOCTYPE, run_name,
        [
            "sync", "status", "items_succeeded", "items_total",
            "preview_plan_json", "error_summary",
        ],
        as_dict=True,
    )
    if not row:
        return {"status": "missing"}

    # Permission check via the parent Sync.
    if not frappe.has_permission(SYNC_DOCTYPE, "read", doc=row.sync):
        frappe.throw(_("Not permitted to read this Product Sync"))

    current = int(row.items_succeeded or 0)
    total = int(row.items_total or 0)
    percent = int(current * 100 / total) if total else 0

    out: dict = {
        "status": row.status or "running",
        "current": current,
        "total": total,
        "percent": min(percent, 99) if row.status == "running" else (
            100 if row.status == "ok" else percent
        ),
    }
    if row.status == "ok" and row.preview_plan_json:
        try:
            import json
            out["plan"] = json.loads(row.preview_plan_json)
        except (ValueError, TypeError):
            out["status"] = "error"
            out["error"] = _("Could not read preview JSON.")
    elif row.status == "error":
        out["error"] = row.error_summary or _("Unknown error.")
    return out


@frappe.whitelist()
def preflight_check(sync: str) -> dict:
    """Mode 1: fast config validation, no backend call.

    Returns ``{ready_for_dryrun: bool, findings: [{severity, code,
    message, count?, fix_hint?}]}``.
    """
    if not frappe.has_permission(SYNC_DOCTYPE, "read", doc=sync):
        frappe.throw(_("Not permitted to read this Product Sync"))

    doc = frappe.get_doc(SYNC_DOCTYPE, sync)
    findings: list[dict] = []

    def _finding(severity: str, code: str, message: str, **extra) -> None:
        findings.append({"severity": severity, "code": code, "message": message, **extra})

    # Backend / channel basics
    if not doc.backend:
        _finding("block", "no_backend", _("Backend ist nicht gesetzt."))
    if not (doc.target_sales_channels or []):
        _finding(
            "warn", "no_channels",
            _("No target storefronts configured — Apply would push nothing."),
        )

    # Scope must resolve to ≥1 Item
    from ecommerce_integrations.product_sync.walker import walk_items_for_sync

    try:
        scope_count = 0
        for _node in walk_items_for_sync(doc):
            scope_count += 1
            if scope_count >= 10000:  # short-circuit huge scopes
                break
        if scope_count == 0:
            _finding(
                "block", "empty_scope",
                _("Scope is empty — no item matches it."),
            )
        else:
            _finding(
                "ok", "scope_size",
                _("Scope contains {0}+ items.").format(scope_count),
                count=scope_count,
            )
    except Exception as exc:  # noqa: BLE001
        _finding(
            "block", "scope_error",
            _("Scope resolution failed: {0}").format(exc),
        )

    # Custom Filter without rules is a block
    if doc.scope_mode == "Custom Filter" and not (doc.custom_filter_rules or []):
        _finding(
            "block", "custom_filter_empty",
            _("Modus 'Custom Filter' verlangt mindestens eine Filter-Regel."),
        )

    # Active flag
    if not int(doc.is_active or 0):
        _finding(
            "warn", "inactive",
            _("Sync is inactive — cron would skip it."),
            fix_hint=_("Setze 'Aktiv' bevor du auf Cron umschaltest."),
        )

    # Per-channel sanity
    primary_count = sum(1 for c in (doc.target_sales_channels or []) if int(c.is_primary or 0))
    if primary_count > 1:
        _finding(
            "block", "multiple_primary",
            _("Mehrere Storefronts sind als 'primary' markiert. Genau einen wählen."),
        )

    # Tax template when sync_taxes=1
    if int(doc.sync_taxes or 0) and not doc.tax_template:
        _finding(
            "warn", "no_tax_template",
            _("Steuer-Synchronisation aktiv, aber keine Steuer-Vorlage gesetzt."),
        )

    # Price strategy sanity
    if doc.price_strategy == "channel_price_list" and not doc.price_list_override:
        all_channels_have_pl = all(
            (c.override_price_list or "").strip()
            for c in (doc.target_sales_channels or [])
        )
        if not all_channels_have_pl:
            _finding(
                "warn", "no_price_list",
                _("Keine globale Price List gesetzt und nicht alle Storefronts haben eine eigene."),
            )

    has_block = any(f["severity"] == "block" for f in findings)
    return {"ready_for_dryrun": not has_block, "findings": findings}


# ─── Reconciliation / Adopt-by-SKU ────────────────────────────────────


@frappe.whitelist()
def auto_adopt_by_sku(
    sync: str,
    *,
    also_try_ean: bool = True,
) -> dict:
    """Walk this Sync's scope and try to adopt existing backend products
    by exact SKU match (and optionally EAN).

    Writes a ``tabEcommerce Item`` row per match so the next preview
    sees the items as already-mapped and produces a clean "what would
    change" diff instead of "create everything".

    Idempotent: items that already have a mapping are skipped, and
    the same SKU is only adopted once.

    Returns ``{adopted, skipped_already_mapped, no_match, ambiguous}``.
    """
    if not frappe.has_permission(SYNC_DOCTYPE, "write", doc=sync):
        frappe.throw(_("Not permitted to adopt for this Product Sync"))

    from ecommerce_integrations.product_sync.engine.adapters.base import AdapterError
    from ecommerce_integrations.product_sync.engine.registry import get_adapter
    from ecommerce_integrations.product_sync.walker import walk_items_for_sync

    doc = frappe.get_doc(SYNC_DOCTYPE, sync)
    integration_key = BACKEND_TO_INTEGRATION_KEY.get(
        doc.backend or "", (doc.backend or "").lower(),
    )

    try:
        adapter = get_adapter(doc.backend)
    except AdapterError as exc:
        frappe.throw(_("Backend-Adapter unavailable: {0}").format(exc))

    counts = {
        "adopted": 0,
        "skipped_already_mapped": 0,
        "no_match": 0,
        "ambiguous": 0,
    }

    # Pre-load existing mappings to skip already-mapped items in O(N).
    item_codes_in_scope = [n.item_code for n in walk_items_for_sync(doc)]
    if not item_codes_in_scope:
        return counts
    existing = set(
        r["erpnext_item_code"]
        for r in frappe.get_all(
            _ECOMMERCE_ITEM_DOCTYPE,
            filters={
                "erpnext_item_code": ("in", item_codes_in_scope),
                "integration": integration_key,
            },
            fields=["erpnext_item_code"],
            limit=0,
        )
    )

    # Collect (item_code, sku, ean) for every unmapped item — one
    # in-memory pass, then two batched HTTP roundtrips.
    pending: list[tuple[str, str, str | None]] = []
    for item_code in item_codes_in_scope:
        if item_code in existing:
            counts["skipped_already_mapped"] += 1
            continue
        item = frappe.get_cached_doc("Item", item_code)
        sku = (item.item_code or "").strip()
        if not sku:
            counts["no_match"] += 1
            continue
        ean = _resolve_ean(item) if also_try_ean else None
        pending.append((item_code, sku, ean))

    if not pending:
        frappe.db.commit()  # noqa: SLF001
        return counts

    sku_list = [sku for (_ic, sku, _e) in pending]
    ean_list = [e for (_ic, _s, e) in pending if e]

    try:
        bulk_matches = adapter.find_matching_products_bulk(
            skus=sku_list,
            eans=ean_list or None,
        )
    except NotImplementedError:
        frappe.throw(_("Adapter does not support find_matching_products_bulk yet."))
    except AdapterError as exc:
        frappe.throw(_("Bulk match search failed: {0}").format(exc))

    for item_code, sku, ean in pending:
        match = bulk_matches.get(sku)
        if match is None and ean:
            match = bulk_matches.get(ean)
        if match is None:
            counts["no_match"] += 1
            continue
        _write_ecommerce_item(
            item_code, integration_key, match.external_id, match.sku or sku,
        )
        counts["adopted"] += 1

    frappe.db.commit()  # noqa: SLF001 — persist before returning
    return counts


def _resolve_ean(item) -> str | None:
    """Return the first EAN-typed barcode on the Item, or None."""
    for row in (getattr(item, "barcodes", []) or []):
        bt = (getattr(row, "barcode_type", "") or "").upper()
        bc = (getattr(row, "barcode", "") or "").strip()
        if bt == "EAN" and bc:
            return bc
    # Fallback: a barcode without a type that looks numeric+13 chars.
    for row in (getattr(item, "barcodes", []) or []):
        bc = (getattr(row, "barcode", "") or "").strip()
        if bc and bc.isdigit() and len(bc) in (8, 13, 14):
            return bc
    return None


def _write_ecommerce_item(
    item_code: str, integration_key: str, external_id: str, sku: str,
) -> None:
    """Upsert a ``tabEcommerce Item`` row with the adoption mapping."""
    existing = frappe.db.exists(
        _ECOMMERCE_ITEM_DOCTYPE,
        {"erpnext_item_code": item_code, "integration": integration_key},
    )
    if existing:
        doc = frappe.get_doc(_ECOMMERCE_ITEM_DOCTYPE, existing)
        doc.integration_item_code = external_id
        # We do NOT touch last_synced_hash here — the next preview
        # will detect the hash drift between ERP and backend and
        # surface it as an update with the actual field diffs.
        doc.flags.ignore_permissions = True
        doc.save()
    else:
        doc = frappe.get_doc({
            "doctype": _ECOMMERCE_ITEM_DOCTYPE,
            "erpnext_item_code": item_code,
            "integration": integration_key,
            "integration_item_code": external_id,
            "sku": sku,
        })
        doc.flags.ignore_permissions = True
        doc.insert()


# ─── Apply Live / Sandbox / Subset ───────────────────────────────────


@frappe.whitelist()
def apply_live(sync: str, *, with_snapshot: bool = True) -> dict:
    """Run a live apply: pushes the diff via the registered adapter
    and persists an ``Ecommerce Sync Run`` audit row.

    Returns the in-process result of :func:`apply_sync` with
    ``dry_run=False``. The ``with_snapshot`` flag is reserved for the
    rollback path that captures the pre-apply backend state.
    """
    if not frappe.has_permission(SYNC_DOCTYPE, "write", doc=sync):
        frappe.throw(_("Not permitted to apply this Product Sync"))
    from ecommerce_integrations.product_sync.tasks import apply_sync

    result = apply_sync(sync, dry_run=False)
    return {
        "sync": result.sync,
        "status": result.status,
        "message": result.message,
    }


@frappe.whitelist()
def run_sandbox(
    sync: str,
    channel_id: str,
    *,
    subset_mode: str = "random_n",
    subset_value: str = "100",
    auto_rollback_on_error: bool = True,
) -> dict:
    """Phase-5 stub: starts a sandbox push against an ``is_sandbox=1``
    channel. Phase-1 returns a placeholder result.
    """
    if not frappe.has_permission(SYNC_DOCTYPE, "write", doc=sync):
        frappe.throw(_("Not permitted to push via sandbox"))
    return {
        "sync": sync,
        "status": "stub",
        "message": _("Sandbox-Push wird in Phase 5 implementiert."),
    }


@frappe.whitelist()
def run_subset(
    sync: str,
    channel_id: str,
    *,
    subset_mode: str = "random_n",
    subset_value: str = "5",
) -> dict:
    """Phase-5 stub: subset push to any channel (production-gated by
    the UI's confirm-typing dance)."""
    if not frappe.has_permission(SYNC_DOCTYPE, "write", doc=sync):
        frappe.throw(_("Not permitted to push subset"))
    return {
        "sync": sync,
        "status": "stub",
        "message": _("Subset-Push wird in Phase 5 implementiert."),
    }


@frappe.whitelist()
def cancel_run(run: str) -> dict:
    """Set ``cancel_requested=1`` on the Run; the apply loop reads it
    between batches and exits with status='partial'."""
    if not frappe.has_permission(_RUN_DOCTYPE, "write", doc=run):
        frappe.throw(_("Not permitted to cancel this Run"))
    doc = frappe.get_doc(_RUN_DOCTYPE, run)
    if doc.status != "running":
        return {"cancelled": False, "reason": _("Run is not in 'running' state")}
    doc.cancel_requested = 1
    doc.flags.ignore_permissions = True
    doc.save()
    return {"cancelled": True}


# ─── Audit / History / Compare / Export ──────────────────────────────


@frappe.whitelist()
def list_test_history(sync: str, *, limit: int = 10) -> list[dict]:
    """Recent Sync Runs for one Sync — populates the Test-History
    dialog. Returns a flat list shape ready for the JS renderer."""
    if not frappe.has_permission(SYNC_DOCTYPE, "read", doc=sync):
        frappe.throw(_("Not permitted to read history for this Sync"))
    rows = frappe.get_all(
        _RUN_DOCTYPE,
        filters={"sync": sync},
        fields=[
            "name", "mode", "status", "started_at", "finished_at",
            "duration_ms", "items_total", "items_succeeded",
            "items_failed", "items_skipped", "subset_mode", "subset_value",
            "is_sandbox", "target_channel_id",
        ],
        order_by="started_at desc",
        limit=int(limit),
    )
    return rows


@frappe.whitelist()
def compare_runs(run_a: str, run_b: str) -> dict:
    """Side-by-side count delta between two Runs of the same Sync."""
    if not frappe.has_permission(_RUN_DOCTYPE, "read", doc=run_a):
        frappe.throw(_("Not permitted to read run_a"))
    if not frappe.has_permission(_RUN_DOCTYPE, "read", doc=run_b):
        frappe.throw(_("Not permitted to read run_b"))
    a = frappe.get_doc(_RUN_DOCTYPE, run_a)
    b = frappe.get_doc(_RUN_DOCTYPE, run_b)
    if a.sync != b.sync:
        frappe.throw(_("Runs belong to different Syncs — cannot compare."))
    return {
        "sync": a.sync,
        "run_a": _run_summary(a),
        "run_b": _run_summary(b),
        "delta_items_total": (b.items_total or 0) - (a.items_total or 0),
        "delta_failed": (b.items_failed or 0) - (a.items_failed or 0),
        "delta_succeeded": (b.items_succeeded or 0) - (a.items_succeeded or 0),
    }


def _run_summary(run) -> dict:
    return {
        "name": run.name,
        "mode": run.mode,
        "status": run.status,
        "started_at": str(run.started_at) if run.started_at else None,
        "duration_ms": run.duration_ms or 0,
        "items_total": run.items_total or 0,
        "items_succeeded": run.items_succeeded or 0,
        "items_failed": run.items_failed or 0,
        "items_skipped": run.items_skipped or 0,
    }


@frappe.whitelist()
def export_run(run: str, format: str = "json") -> dict:
    """Phase-5 stub: serialise the Run's preview_plan / applied_diffs
    as csv/json/pdf for compliance audits."""
    if not frappe.has_permission(_RUN_DOCTYPE, "read", doc=run):
        frappe.throw(_("Not permitted to export this Run"))
    doc = frappe.get_doc(_RUN_DOCTYPE, run)
    if format not in ("json", "csv", "pdf"):
        frappe.throw(_("Unsupported export format: {0}").format(format))
    return {
        "run": doc.name,
        "format": format,
        "preview_plan_json": doc.preview_plan_json or "",
        "applied_diffs_json": doc.applied_diffs_json or "",
    }


@frappe.whitelist()
def rollback_run(run: str) -> dict:
    """Phase-5 stub: re-push the snapshot_json so the backend reverts
    to the state captured before this Run."""
    if not frappe.has_permission(_RUN_DOCTYPE, "write", doc=run):
        frappe.throw(_("Not permitted to roll back this Run"))
    return {
        "run": run,
        "status": "stub",
        "message": _("Rollback wird in Phase 5 implementiert."),
    }


@frappe.whitelist()
def cleanup_sandbox(run: str) -> dict:
    """Phase-5 stub: delete/deactivate items previously pushed to a
    sandbox channel via this Run's snapshot."""
    if not frappe.has_permission(_RUN_DOCTYPE, "write", doc=run):
        frappe.throw(_("Not permitted to clean up this Run"))
    return {
        "run": run,
        "status": "stub",
        "message": _("Sandbox-Cleanup wird in Phase 5 implementiert."),
    }


# ─── Cross-sync queries (dashboards / setting widgets) ───────────────


@frappe.whitelist()
def list_for_backend(backend: str) -> list[dict]:
    """All Product Syncs for one backend — for the Setting-form widget
    and the Workspace ChannelHealth panel."""
    if not frappe.has_permission(SYNC_DOCTYPE, "read"):
        frappe.throw(_("Not permitted to read Product Syncs"))
    if backend not in (BACKEND_SHOPWARE, BACKEND_MEDUSA):
        frappe.throw(_("Unknown backend: {0}").format(backend))
    return frappe.get_all(
        SYNC_DOCTYPE,
        filters={"backend": backend},
        fields=[
            "name", "title", "is_active", "scope_mode", "priority",
            "sync_status", "last_synced_at", "items_in_scope",
            "items_synced", "items_failed",
        ],
        order_by="priority desc, modified desc",
    )


# ─── Helpers ─────────────────────────────────────────────────────────


def _force_db_reconnect() -> None:
    """Hard-reset the worker's MySQL handle.

    ``frappe.connect()`` short-circuits when ``frappe.local.db`` is
    already populated, so a stale handle survives a plain ``close()``
    + ``connect()`` pair. Setting ``frappe.local.db = None`` first
    forces a real fresh connection — required after a multi-minute
    HTTPS-bound fetch tripped MySQL's ``wait_timeout``.
    """
    try:
        frappe.db.close()
    except Exception:  # noqa: BLE001
        pass
    frappe.local.db = None
    frappe.connect(site=getattr(frappe.local, "site", None))


def _set_value_with_db_retry(doctype: str, name: str, values: dict, **kwargs) -> None:
    """``frappe.db.set_value`` with one auto-reconnect retry.

    Wraps the single error we keep hitting on long-running preview
    workers — ``(2006, 'Server has gone away')`` after the connection
    sat idle through an HTTPS-bound drift fetch. One retry after a
    forced reconnect is enough; if it fails twice the underlying
    issue isn't the idle timeout and we let it raise.
    """
    try:
        frappe.db.set_value(doctype, name, values, **kwargs)
        return
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        if "gone away" not in msg and "2006" not in msg and "2013" not in msg:
            raise
        _force_db_reconnect()
        frappe.db.set_value(doctype, name, values, **kwargs)


def _coerce_int(v) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _coerce_bool(v, *, default: bool) -> bool:
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off", ""):
        return False
    return default
