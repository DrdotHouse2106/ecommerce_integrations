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

# Apply-loop batch size — products go through the backend's bulk
# endpoint (Shopware ``_action/sync``, Medusa ``/admin/products/batch``)
# in chunks of this size. synQup's Shopware connector docs recommend
# ~25 for the sync endpoint because products are complex entities and
# big batches risk internal-server-errors / memory pressure on the
# target shop. Each batch is one HTTP roundtrip → on a 33 961-item
# apply this drops from 33 961 round trips to ~1 359.
_APPLY_BATCH_SIZE = 25

# How long a Sync Run may stay in ``running`` without a heartbeat
# before ``recover_stale_product_syncs`` flips it to ``error``. The
# scheduler runs hourly, so 30 min is a comfortable lower bound
# (matches the Catalog Mirror / Smart Collections sweepers).
_HEARTBEAT_TIMEOUT_MIN = 30


def _logger():
    """Module logger for apply-live progress. Lands in bench logs."""
    return frappe.logger("product_sync.apply", allow_site=True, file_count=10)


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
        fetch_live=fetch_live,
        mode=mode,
        triggered_by=triggered_by or frappe.session.user,
        trigger_type=trigger_type,
    )


def _apply_live(
    sync_doc,
    *,
    subset_item_codes: list[str] | None = None,
    fetch_live: bool = False,
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
    ai_generated_count = 0
    try:
        # 0. Optional: pre-pass to fill missing AI descriptions so the
        #    diff sees the fresh copy and the apply step pushes content
        #    instead of empty strings. Bounded by ``max_ai_pre_generate``
        #    so a 33k-item Sync can't accidentally torch the token
        #    budget on its first run.
        if (
            (sync_doc.description_source or "") == "ai_generated"
            and int(getattr(sync_doc, "auto_generate_ai_descriptions", 0) or 0)
        ):
            ai_generated_count = _ai_pre_pass(
                sync_doc, subset_item_codes=subset_item_codes,
            )

        # 1. Build the diff (full set, no max_items cap for apply).
        last_diff_progress_write = [0.0]

        def _on_diff_progress(current: int, total: int) -> None:
            now_ts = time.time()
            if (now_ts - last_diff_progress_write[0]) < 1.0 and current < total:
                return
            last_diff_progress_write[0] = now_ts
            # ``update_modified=True`` is load-bearing here: the stale-run
            # sweeper compares ``modified`` against the heartbeat timeout,
            # so without bumping it a hung diff phase stays invisible.
            frappe.db.set_value(
                _RUN_DOCTYPE, run.name,
                {
                    "items_total": total,
                    "items_succeeded": current,
                },
                update_modified=True,
            )
            # Mirror the heartbeat onto the parent Sync row so the cron
            # sweeper's parent-level check also catches a stuck diff.
            frappe.db.set_value(
                SYNC_DOCTYPE, sync_doc.name,
                {"last_heartbeat_at": now_datetime()},
                update_modified=False,
            )
            frappe.db.commit()  # make live-diff progress visible to the UI

        plan = compute_product_diff(
            sync_doc,
            max_items=None,
            fetch_live=fetch_live,
            subset_item_codes=subset_item_codes,
            on_progress=_on_diff_progress,
        )
        # Persist the plan up-front so the operator can see what was
        # intended even if the apply loop crashes halfway. Use
        # db.set_value to avoid the check_if_latest race with later
        # _finalize_run writes (same row, same writer).
        #
        # On a 5000+-item plan the raw JSON dump can exceed MariaDB's
        # ``max_allowed_packet`` and the resulting ``(1153)`` kills the
        # DB connection mid-apply. Route through ``_maybe_gzip_plan_json``
        # (typical 12 MB → ~1 MB) — recoverable via
        # ``_maybe_gunzip_plan_json`` on read. Wrapped in try/except so a
        # future packet-limit edge case logs a warning instead of
        # aborting the apply.
        # Cron-triggered runs skip ``preview_plan_json`` persistence —
        # serialising 37k+ ``ProductNodePlan`` rows to indented JSON
        # ate minutes of wall time and gigabytes of RSS before any
        # apply could happen. The UI's "what was planned" view is the
        # only consumer of ``preview_plan_json``; cron has no UI and
        # the per-item outcome is already audited via the
        # ``applied_diffs`` rows written during apply. Manual desk
        # runs still get the full plan dump.
        items_total_post_diff = len(plan.creates) + len(plan.updates)
        if trigger_type == "cron":
            try:
                frappe.db.set_value(
                    _RUN_DOCTYPE, run.name,
                    {"items_total": items_total_post_diff},
                    update_modified=False,
                )
            except Exception:  # noqa: BLE001
                pass
        else:
            try:
                from ecommerce_integrations.product_sync.api import _maybe_gzip_plan_json
                plan_json = frappe.as_json(plan.to_dict(), indent=1)
                stored = _maybe_gzip_plan_json(plan_json)
                frappe.db.set_value(
                    _RUN_DOCTYPE, run.name,
                    {
                        "preview_plan_json": stored,
                        "items_total": items_total_post_diff,
                    },
                    update_modified=False,
                )
            except Exception as exc:  # noqa: BLE001 — never let plan persistence kill the apply
                _logger().error(
                    f"apply-live: failed to persist preview_plan_json for "
                    f"sync={sync_doc.name} run={run.name}: {type(exc).__name__}: {exc}"
                )
                try:
                    frappe.db.set_value(
                        _RUN_DOCTYPE, run.name,
                        {"items_total": items_total_post_diff},
                        update_modified=False,
                    )
                except Exception:  # noqa: BLE001
                    pass

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

        # 2.5: ensure brand manufacturer rows ONCE per run (not per
        # batch). Brand data — logo + description + link — doesn't
        # vary across batches of the same run, and the upload step
        # (URL fetch of the logo) is the most expensive part. Hashing
        # the per-brand state and comparing against ``Brand`` custom
        # field ``last_synced_brand_hash`` means we only re-push when
        # the operator actually changed something on the ERPNext
        # side.
        if backend == BACKEND_SHOPWARE:
            try:
                _ensure_brands_for_run(
                    sync_doc=sync_doc, plan=plan, adapter=adapter,
                    run_name=run.name,
                )
            except Exception as exc:  # noqa: BLE001
                _logger().warning(
                    f"brand pre-flight failed for sync={sync_doc.name} "
                    f"run={run.name}: {type(exc).__name__}: {exc}"
                )

        # 3. Apply creates + updates in batches against the backend's
        #    bulk endpoint (Shopware /_action/sync, Medusa
        #    /admin/products/batch). Per-item HTTP would be 33k+ round
        #    trips on a five-figure catalogue; batched calls keep the
        #    apply pass tractable. Batch size is conservative (25)
        #    because products are complex entities — synQup's
        #    recommendation for Shopware sync API stability.
        to_process: list[tuple] = []
        for node in plan.creates:
            to_process.append((node, ACTION_CREATE))
        for node in plan.updates:
            to_process.append((node, ACTION_UPDATE))

        total_to_process = len(to_process)
        # Reset the visible progress counters at the diff → apply
        # transition. Without this the Run row keeps showing the
        # diff-phase ``items_succeeded`` value (which counted items
        # *walked*, not items *applied*) until the very end of the
        # run — looking exactly like the worker is hung. Pin
        # ``items_total`` to the actual apply scope here so the UI
        # ratio matches what's about to happen.
        try:
            frappe.db.set_value(
                _RUN_DOCTYPE, run.name,
                {
                    "items_total": total_to_process,
                    "items_succeeded": 0,
                    "created_count": 0,
                    "updated_count": 0,
                },
                update_modified=True,
            )
            # Commit so the UI sees the phase transition immediately
            # instead of having to wait for the worker-job-level commit
            # at the end of apply.
            frappe.db.commit()
        except Exception:  # noqa: BLE001 — never crash apply on a counter write
            pass
        _logger().info(
            f"apply-live start: sync={sync_doc.name} backend={backend} "
            f"to_process={total_to_process} batch_size={_APPLY_BATCH_SIZE} run={run.name}"
        )
        last_progress_write = [0.0]
        for batch_start in range(0, total_to_process, _APPLY_BATCH_SIZE):
            batch = to_process[batch_start:batch_start + _APPLY_BATCH_SIZE]
            # Heartbeat + cancel check between batches.
            if _is_cancelled(run.name):
                result.message = _("Run wurde durch den Operator abgebrochen.")
                _logger().info(
                    f"apply-live cancelled: sync={sync_doc.name} run={run.name} "
                    f"at item {batch_start}/{total_to_process}"
                )
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
            _publish_progress(
                sync_doc.name, run.name, batch_start, total_to_process, result,
            )
            # Persist apply-phase counters to the Run row so the UI sees
            # live progress between batches (the diff-phase progress
            # writer doesn't run during apply). Throttled to 1 Hz to
            # avoid hammering ``modified`` on a fast bulk endpoint.
            now_ts = time.time()
            if (now_ts - last_progress_write[0]) >= 1.0 or batch_start == 0:
                last_progress_write[0] = now_ts
                try:
                    frappe.db.set_value(
                        _RUN_DOCTYPE, run.name,
                        {
                            "items_succeeded": result.created + result.updated,
                            "created_count": result.created,
                            "updated_count": result.updated,
                            "items_failed": len(result.errors),
                        },
                        update_modified=True,
                    )
                    # Commit so the UI sees per-batch progress; without
                    # the explicit commit the writes stay in the
                    # worker's job-level transaction and only land at
                    # the end of the apply.
                    frappe.db.commit()
                except Exception:  # noqa: BLE001
                    pass
            # Per-batch INFO log — lets us locate the last known position
            # in bench logs when the worker is killed (OOM, SIGKILL) before
            # the top-level except handler can write to Error Log.
            _logger().info(
                f"apply-live batch: sync={sync_doc.name} run={run.name} "
                f"{batch_start}/{total_to_process} "
                f"(created={result.created} updated={result.updated} "
                f"skipped={result.skipped} errs={len(result.errors)})"
            )

            _apply_batch(
                batch=batch,
                sync_doc=sync_doc,
                adapter=adapter,
                target_channels=target_channels,
                integration_key=integration_key,
                run_name=run.name,
                backend=backend,
                applied_diffs=applied_diffs,
                result=result,
            )

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

        # 6. Smart Collection category links. Product upsert creates/updates
        #    the Medusa/Shopware product mappings; Smart Collection targets
        #    own the category-product link table, so rerun the linked
        #    collections after products exist. This keeps Smart-Collection
        #    scoped Product Syncs category-complete without a second manual
        #    click.
        _sync_linked_smart_collection_categories(sync_doc, result)

        # 7. Final status.
        status = STATUS_OK if not result.errors else STATUS_PARTIAL
        _finalize_run(
            run, result, status=status,
            message=None, applied_diffs=applied_diffs,
            started_ts=started_ts,
            ai_generated=ai_generated_count,
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
        _logger().error(
            f"apply-live crashed: sync={sync_doc.name} run={run.name} {msg}\n{tb}"
        )
        _finalize_run(
            run, result, status=STATUS_ERROR,
            message=msg, applied_diffs=applied_diffs, started_ts=started_ts,
        )
        _release_sync(sync_doc.name, status=STATUS_ERROR, last_error=msg)
        frappe.log_error(title=f"Product Sync apply crashed: {sync_doc.name}", message=tb)
        result.status = STATUS_ERROR
        result.message = msg
        return result


def _sync_linked_smart_collection_categories(sync_doc, result: ProductSyncRunResult) -> None:
    """Refresh category-product links for Smart Collection scoped syncs.

    The Product Sync payload links products to Item Group categories only.
    For Smart Collections, the backend category and its product links are
    managed by the Smart Collections module, which resolves ERP item codes
    to the freshly written Ecommerce Item mappings. Running it here after
    product upsert makes the manual Product Sync end-to-end for Medusa and
    Shopware without blocking the browser.
    """
    if (sync_doc.scope_mode or "") != "Smart Collection":
        return

    names: list[str] = []
    seen: set[str] = set()

    legacy = (getattr(sync_doc, "linked_smart_collection", None) or "").strip()
    if legacy:
        names.append(legacy)
        seen.add(legacy)

    for row in (getattr(sync_doc, "linked_smart_collections", None) or []):
        name = (row.smart_collection or "").strip()
        if name and name not in seen:
            names.append(name)
            seen.add(name)

    if not names:
        return

    from ecommerce_integrations.smart_collections.tasks import sync_collection

    ok = 0
    for name in names:
        try:
            outcome = sync_collection(name)
            targets = outcome.get("targets") if isinstance(outcome, dict) else []
            if any(t.get("status") == "error" for t in (targets or [])):
                result.errors.append(f"smart collection {name}: target sync error")
            else:
                ok += 1
        except Exception as exc:  # noqa: BLE001
            result.errors.append(f"smart collection {name}: {exc}")

    if ok:
        msg = _("Smart Collection category links refreshed: {0}.").format(ok)
        result.message = (result.message + " " if result.message else "") + msg


# ─── Per-item application ────────────────────────────────────────────


def _apply_batch(
    *,
    batch: list[tuple],
    sync_doc,
    adapter,
    target_channels: list[str],
    integration_key: str,
    run_name: str,
    backend: str,
    applied_diffs: list[dict],
    result,
) -> None:
    """Push a batch of items to the backend in one bulk-API roundtrip.

    Builds payloads + hashes for every item in ``batch``, calls
    ``adapter.upsert_products_bulk(...)`` once, then loops the per-item
    results to persist mappings, append to ``applied_diffs``, and bump
    the run counters. Any per-item error is captured in the result
    dict so a single bad item can't take down the whole batch.

    Adapter responsibility: the bulk call MUST return one result row
    per input item (same order). The base ``ProductAdapter`` enforces
    this via a default fallback to per-item upsert, so the contract
    holds even for backends that haven't implemented bulk yet.
    """
    from ecommerce_integrations.product_sync.engine.payload import (
        build_medusa_payload,
        build_shopware_payload,
    )

    # Phase A: build per-item payloads + capture state we'll need after
    # the bulk call returns.
    prepared: list[dict] = []
    metadata: list[dict] = []  # parallel list — same indices as prepared
    for node, action in batch:
        try:
            item = frappe.get_doc("Item", node.item_code)
        except frappe.DoesNotExistError:
            applied_diffs.append({
                "item_code": node.item_code,
                "action": action,
                "status": "error",
                "error": "Item not found",
            })
            result.errors.append(f"{node.item_code}: Item not found")
            continue
        canonical = build_canonical_payload(item, sync_doc)
        proposed_hash = compute_hash(canonical)
        ecom_row = frappe.db.get_value(
            _ECOMMERCE_ITEM_DOCTYPE,
            {"erpnext_item_code": node.item_code, "integration": integration_key},
            ["name", "integration_item_code", "variant_id", "last_synced_canonical"],
            as_dict=True,
        )
        external_id = ecom_row.get("integration_item_code") if ecom_row else None
        variant_id = ecom_row.get("variant_id") if ecom_row else None

        # Per-field delta: decode the stored canonical (if any) and
        # compute which sections actually changed. The first push for
        # a mapped item, and creates (no external_id), always send the
        # full payload — ``stored_canonical=None`` makes
        # ``changed_sections`` return every section in ``proposed``.
        from ecommerce_integrations.product_sync.engine.canonical import (
            changed_sections as _changed_sections,
            decode_canonical,
        )
        stored_canonical = decode_canonical(
            (ecom_row or {}).get("last_synced_canonical")
        )
        delta_sections: set[str] = _changed_sections(stored_canonical, canonical)
        # On CREATE (no external_id) the adapter needs every field —
        # Shopware's sync endpoint treats missing keys as "no change"
        # which on insert means "empty value". Force the full payload.
        partial_sections = None if not external_id else delta_sections

        if backend == BACKEND_SHOPWARE:
            payload = build_shopware_payload(
                item, sync_doc, canonical,
                external_id=external_id,
                changed_sections=partial_sections,
            )
        elif backend == BACKEND_MEDUSA:
            payload = build_medusa_payload(
                item, sync_doc, canonical,
                external_id=external_id, variant_id=variant_id,
            )
        else:
            applied_diffs.append({
                "item_code": node.item_code,
                "action": action,
                "status": "error",
                "error": f"Unknown backend: {backend}",
            })
            result.errors.append(f"{node.item_code}: unknown backend")
            continue

        prepared.append({
            "external_id": external_id,
            "payload": payload,
        })
        metadata.append({
            "item_code": node.item_code,
            "action": action,
            "ecom_row": ecom_row,
            "proposed_hash": proposed_hash,
            # Stash the freshly-loaded Item doc + canonical payload so
            # Phase C's image / variant-configurator branches can reuse
            # them instead of issuing two more ``frappe.get_doc("Item",
            # ...)`` calls per item (and ``build_canonical_payload``
            # again for the image URLs). On a 37k batched apply the
            # redundant loads dominated wall-time — most of the apply
            # phase was Item-doc reads, not Shopware roundtrips.
            "item_doc": item,
            "canonical": canonical,
            # The set of canonical sections that actually changed —
            # used by Phase C to skip enrichments whose source section
            # is unchanged (e.g., skip property push when ``properties``
            # is not in the set). On CREATE the set is irrelevant
            # because we send the full payload.
            "delta_sections": delta_sections,
        })

    if not prepared:
        return

    # Phase A.5a: brand entities are now ensured ONCE per apply run
    # (see ``_ensure_brands_for_run`` invoked from ``_apply_live``
    # before the batch loop), not per batch — the legacy
    # per-batch call here re-uploaded logos thousands of times for a
    # 37k-item catalogue. Skipped here.

    # Phase A.5b: pre-flight ensure ``property_group`` +
    # ``property_group_option`` entities exist for every
    # ``ecommerce_property`` AND every variant ``attribute`` we're
    # about to push as a nested m2m on the products. Two batched
    # ``_action/sync`` calls (groups then options) replace what used
    # to be N×search + N×upsert HTTP roundtrips inside the per-item
    # ``push_product_properties`` / ``push_variant_options`` Phase C
    # helpers. Skipped for non-Shopware backends.
    if backend == BACKEND_SHOPWARE and hasattr(adapter, "ensure_property_options_bulk"):
        try:
            groups_set: set[tuple[str, str]] = set()  # (name, kind)
            options_set: set[tuple[str, str, str]] = set()  # (group, value, kind)
            for meta in metadata:
                props_canonical = (meta.get("canonical") or {}).get("properties") or {}
                for p in (props_canonical.get("ecommerce_properties") or []):
                    name = (p.get("name") or "").strip()
                    value = (p.get("value") or "").strip()
                    if name and value:
                        groups_set.add((name, "property"))
                        options_set.add((name, value, "property"))
                # Variant attributes belong to the variant items only.
                item_doc = meta.get("item_doc")
                if item_doc and getattr(item_doc, "variant_of", None):
                    for a in (props_canonical.get("attributes") or []):
                        name = (a.get("name") or "").strip()
                        value = (a.get("value") or "").strip()
                        if name and value:
                            groups_set.add((name, "variant"))
                            options_set.add((name, value, "variant"))
            if groups_set:
                adapter.ensure_property_entities_bulk(sorted(groups_set))
            if options_set:
                adapter.ensure_property_options_bulk(sorted(options_set))
        except Exception as exc:  # noqa: BLE001 — pre-flight is best-effort
            _logger().warning(
                f"property entity pre-flight failed for sync={sync_doc.name} "
                f"run={run_name}: {type(exc).__name__}: {exc}"
            )

    # Phase B: one bulk call. The adapter handles chunking inside if
    # the batch is larger than its API allows.
    try:
        results = adapter.upsert_products_bulk(
            prepared, target_sales_channels=target_channels,
        )
    except Exception as exc:  # noqa: BLE001
        # Whole-batch failure: every item gets the same error so the
        # operator sees one clean reason in the audit row instead of N
        # duplicated lines.
        for meta in metadata:
            _record_item_failure(
                sync_doc.name, meta["item_code"], backend, exc,
                payload=None, response=None, run_name=run_name,
            )
            result.errors.append(f"{meta['item_code']}: {exc}")
            applied_diffs.append({
                "item_code": meta["item_code"],
                "action": meta["action"],
                "status": "error",
                "error": str(exc),
            })
        return

    # Phase C: walk per-item results, persist mappings + counters.
    #
    # One Shopware session pinned for the whole batch so the per-item
    # adapter helpers (``reconcile_product_media``,
    # ``push_product_properties``, ``push_variant_options``,
    # ``push_template_configurator``, ``upload_product_images``)
    # share a single authenticated client instead of doing a fresh
    # OAuth handshake each call. On a 37k apply that's the
    # difference between "per-item OAuth round-trip" and "one
    # handshake per ~25 items".
    #
    # An earlier revert blamed shared sessions for empty media
    # records, but the actual root cause was Shopware's MinIO
    # storage backend filling up (HTTP 507 on file write) — the
    # session reuse was orthogonal. The idempotency-key rotator in
    # ``_with_client`` ensures distinct writes on the shared client
    # get distinct keys, so Shopware doesn't dedupe them as retries.
    if backend == BACKEND_SHOPWARE:
        from ecommerce_integrations.product_sync.engine.adapters.shopware import (
            shared_shopware_session,
        )
        _phase_c_session = shared_shopware_session()
        _phase_c_session.__enter__()
    else:
        _phase_c_session = None
    for meta, row in zip(metadata, results, strict=False):
        err = row.get("error")
        if err:
            _record_item_failure(
                sync_doc.name, meta["item_code"], backend, Exception(err),
                payload=None, response=None, run_name=run_name,
            )
            result.errors.append(f"{meta['item_code']}: {err}")
            applied_diffs.append({
                "item_code": meta["item_code"],
                "action": meta["action"],
                "status": "error",
                "error": err,
            })
            continue

        new_external_id = row.get("external_id") or prepared[
            metadata.index(meta)
        ].get("external_id")
        if not new_external_id:
            # Bulk-API didn't echo an id and we didn't have one. This
            # shouldn't happen for a successful row but the defensive
            # branch keeps us from persisting a broken mapping.
            applied_diffs.append({
                "item_code": meta["item_code"],
                "action": meta["action"],
                "status": "error",
                "error": "Backend returned no external_id",
            })
            result.errors.append(
                f"{meta['item_code']}: no external_id from backend",
            )
            continue

        _persist_item_success(
            item_code=meta["item_code"],
            integration=integration_key,
            external_id=new_external_id,
            proposed_hash=meta["proposed_hash"],
            run_name=run_name,
            existing_row=meta["ecom_row"],
            canonical=meta.get("canonical"),
        )
        # After the upsert, push images via the backend's dedicated
        # two-step media flow (Shopware needs an explicit
        # ``/_action/media/{id}/upload`` call — the sync endpoint's
        # ``media`` field only creates empty records). Adapters without
        # ``upload_product_images`` are silently skipped.
        # Track whether this item's images are already perfectly in
        # sync with what was last pushed — if so the whole Phase-C
        # image block (upload + reconcile) can be skipped, which is
        # the difference between a delta-aware re-sync and a
        # full-rewalk after every canonical-schema bump.
        #
        # Per-section delta: when the stored canonical's ``images``
        # section is byte-identical to the new ``images`` section, the
        # item didn't drift in this dimension and Phase-C image work
        # can be skipped wholesale. This is the cheap fast-path that
        # turns 37k re-pushes (after a non-image schema bump) into
        # 37k no-ops on the image side.
        _delta_sections = meta.get("delta_sections") or set()
        _images_in_sync = (
            "images" not in _delta_sections and meta.get("ecom_row")
            and bool(meta.get("ecom_row", {}).get("last_synced_canonical"))
        )
        if int(getattr(sync_doc, "sync_images", 0) or 0) and not _images_in_sync:
            uploader = getattr(adapter, "upload_product_images", None)
            if callable(uploader):
                # Build the list of resolved absolute URLs from the same
                # helper the (legacy) media-field code path used.
                from ecommerce_integrations.product_sync.engine.payload import (
                    _build_shopware_media,
                )
                canonical_images = (
                    (meta.get("canonical") or {}).get("images") or []
                )
                media = _build_shopware_media(canonical_images)
                urls = [
                    m.get("media", {}).get("url")
                    for m in media if m.get("media", {}).get("url")
                ]
                # Skip image-upload when every canonical image has
                # already been pushed (basename present in
                # ``pushed_image_map``). Without this guard, every
                # chunked re-sync of a 34k catalogue creates a fresh
                # media record per image — duplicating the
                # ``tabmedia`` rows and bloating Shopware storage on
                # each pass. The differ uses the same map to suppress
                # spurious image-drift, so checking it here keeps the
                # short-circuit symmetric.
                existing_basenames: set[str] = set()
                if urls and meta.get("ecom_row"):
                    try:
                        import json as _json
                        existing_map_raw = frappe.db.get_value(
                            _ECOMMERCE_ITEM_DOCTYPE,
                            meta["ecom_row"]["name"],
                            "pushed_image_map",
                        ) or ""
                        existing_map = (
                            _json.loads(existing_map_raw)
                            if existing_map_raw else {}
                        )
                        existing_basenames = {
                            k.lower() for k in (existing_map or {}).keys()
                        }
                    except (ValueError, TypeError):
                        existing_basenames = set()
                    needed_basenames = {
                        (u.rstrip("/").rsplit("/", 1)[-1].split("?", 1)[0]).lower()
                        for u in urls
                    }
                    if needed_basenames and needed_basenames.issubset(existing_basenames):
                        urls = []  # all already pushed → nothing to do
                        # Exact-match check: if pushed_image_map's
                        # basenames equal the canonical set (not just a
                        # superset), there are no stale media to delete
                        # either — reconcile would be a pure no-op
                        # round-trip. Set the flag so the reconcile
                        # block below skips. Items with new images, OR
                        # items where pushed_image_map has *extra*
                        # entries (potential stale media), still go
                        # through reconcile.
                        if needed_basenames == existing_basenames:
                            _images_in_sync = True
                elif not urls:
                    # Item has no images at all — nothing to upload,
                    # nothing to reconcile. (Items previously synced
                    # with images would have a non-empty
                    # ``pushed_image_map``; if both are empty the item
                    # is truly image-less.)
                    _images_in_sync = True

                if urls:
                    try:
                        upload_result = uploader(new_external_id, urls) or {}
                        # Persist ERP-basename → media-UUID map so the
                        # next preview's image-diff can short-circuit
                        # when the live Shopware media set matches.
                        new_map = upload_result.get("media_map") or {}
                        if new_map and meta.get("ecom_row"):
                            existing_raw = frappe.db.get_value(
                                _ECOMMERCE_ITEM_DOCTYPE,
                                meta["ecom_row"]["name"],
                                "pushed_image_map",
                            ) or ""
                            try:
                                import json as _json
                                merged = _json.loads(existing_raw) if existing_raw else {}
                            except (ValueError, TypeError):
                                merged = {}
                            merged.update(new_map)
                            frappe.db.set_value(
                                _ECOMMERCE_ITEM_DOCTYPE,
                                meta["ecom_row"]["name"],
                                "pushed_image_map",
                                frappe.as_json(merged),
                                update_modified=False,
                            )
                    except Exception as exc:  # noqa: BLE001
                        # Per-image errors are already counted inside
                        # the adapter; only a complete crash gets here.
                        result.errors.append(
                            f"{meta['item_code']}: image upload failed: {exc}",
                        )

                # Delta media reconciliation: delete product_media rows
                # whose media UUID isn't in the (now-updated) pushed
                # image map. Without this the storefront accumulates
                # stale photos across re-syncs — old product
                # photography that the operator replaced, orphans from
                # prior mis-mappings, etc. The cover gets re-pointed
                # automatically if it landed on a deleted row.
                #
                # When ``_images_in_sync`` is set the canonical image
                # set is byte-for-byte the same as what was last
                # pushed — reconcile would issue one Shopware search +
                # zero deletes, which is pure latency. Skipping it
                # here is the difference between "delta sync touches
                # only what changed" and "delta sync re-walks every
                # item's Phase-C every cron tick after a
                # canonical-schema bump invalidates the run-level
                # hash".
                reconciler = getattr(adapter, "reconcile_product_media", None)
                if (
                    callable(reconciler)
                    and meta.get("ecom_row")
                    and not _images_in_sync
                ):
                    try:
                        import json as _json2
                        final_map_raw = frappe.db.get_value(
                            _ECOMMERCE_ITEM_DOCTYPE,
                            meta["ecom_row"]["name"],
                            "pushed_image_map",
                        ) or ""
                        final_map = (
                            _json2.loads(final_map_raw) if final_map_raw else {}
                        )
                        keep_ids = list(final_map.values())
                        if keep_ids:
                            reconciler(new_external_id, keep_ids)
                    except Exception as exc:  # noqa: BLE001
                        result.errors.append(
                            f"{meta['item_code']}: media reconcile failed: {exc}",
                        )

        # Phase-C properties push: now handled via the nested
        # ``properties`` m2m on the bulk product upsert above
        # (``build_shopware_payload`` emits it when canonical's
        # ``ecommerce_properties`` is non-empty, and the Phase-A.5
        # pre-flight ensures the option entities exist). Keeping the
        # per-item push here would just re-PATCH the same association
        # one product at a time — pure latency, no new data.
        #
        # Backends without nested-association support fall back to
        # the per-item push (the conditional below stays true for
        # those — only Shopware ships the new helper).
        _properties_handled_in_bulk = (
            backend == BACKEND_SHOPWARE
            and hasattr(adapter, "ensure_property_options_bulk")
        )
        _properties_in_sync = (
            "properties" not in _delta_sections and meta.get("ecom_row")
            and bool(meta.get("ecom_row", {}).get("last_synced_canonical"))
        )
        if (
            int(getattr(sync_doc, "sync_properties", 0) or 0)
            and not _properties_in_sync
            and not _properties_handled_in_bulk
        ):
            pusher = getattr(adapter, "push_product_properties", None)
            if callable(pusher):
                ecom_props = []
                try:
                    rows = frappe.get_all(
                        "Item Ecommerce Property",
                        filters={
                            "parent": meta["item_code"],
                            "parenttype": "Item",
                            "sync_to_shopware": 1,
                        },
                        fields=["property_name", "property_value"],
                        order_by="idx",
                    )
                    ecom_props = [
                        {"name": r["property_name"], "value": r["property_value"]}
                        for r in rows
                        if r.get("property_name") and r.get("property_value")
                    ]
                except Exception:  # noqa: BLE001
                    ecom_props = []
                if ecom_props:
                    try:
                        pusher(new_external_id, ecom_props)
                    except Exception as exc:  # noqa: BLE001
                        result.errors.append(
                            f"{meta['item_code']}: properties push failed: {exc}",
                        )

        # Variant configurator wiring: when this Item is a variant or
        # a template-with-variants, push the property_group_option link
        # so the storefront renders the variant selector. Without this
        # variants are linked via ``parentId`` but the PDP shows them
        # as standalone single products (no option picker).
        #
        # Per-section delta: variant attribute values live under the
        # ``properties`` canonical section ("attributes" sub-key in
        # ``_canonical_properties``). When that section didn't drift,
        # neither the variant option assignment nor the template
        # configurator needs touching — they're idempotent and
        # already-applied on the live product.
        _variants_in_sync = (
            "properties" not in _delta_sections and meta.get("ecom_row")
            and bool(meta.get("ecom_row", {}).get("last_synced_canonical"))
        )
        if (
            int(getattr(sync_doc, "include_variants", 1) or 0)
            and not _variants_in_sync
        ):
            opt_pusher = getattr(adapter, "push_variant_options", None)
            cfg_pusher = getattr(adapter, "push_template_configurator", None)
            # Reuse the Item doc loaded in Phase A — re-fetching the
            # full doc here was the single biggest contributor to apply
            # wall-time on a 37k catalogue.
            item_doc = meta.get("item_doc")
            if item_doc is not None and callable(opt_pusher) and callable(cfg_pusher):
                variant_of = getattr(item_doc, "variant_of", None) or ""
                has_variants = bool(int(getattr(item_doc, "has_variants", 0) or 0))
                attrs = getattr(item_doc, "attributes", []) or []

                if variant_of and attrs and not _properties_handled_in_bulk:
                    # Variant: set its option ids on the live product.
                    # When the bulk path coalesced ``options`` into the
                    # nested product upsert above we skip the per-item
                    # PATCH — it would just re-write the same m2m link.
                    av = [
                        {"name": a.attribute, "value": a.attribute_value}
                        for a in attrs if a.attribute and a.attribute_value
                    ]
                    if av:
                        try:
                            opt_pusher(new_external_id, av)
                        except Exception as exc:  # noqa: BLE001
                            result.errors.append(
                                f"{meta['item_code']}: variant options push failed: {exc}"
                            )

                if has_variants:
                    # Template: collect the option ids across all
                    # variants, set as ``configuratorSettings`` so the
                    # storefront knows which axes to show on the PDP.
                    #
                    # Bulk-coalesced path: the variant ``options`` m2m
                    # was already written into each variant's payload
                    # via ``build_shopware_payload`` (nested in the
                    # batch product upsert). The option entities were
                    # pre-created by the Phase-A.5 ensure call. So we
                    # don't need to ask Shopware for the option_ids —
                    # we can derive them deterministically from the
                    # canonical attributes via ``property_option_uuid``
                    # without N×N variant API calls.
                    if _properties_handled_in_bulk:
                        from ecommerce_integrations.product_sync.engine.adapters.shopware import (
                            property_option_uuid as _opt_uuid,
                        )
                        variant_attr_rows = frappe.get_all(
                            "Item Variant Attribute",
                            filters={
                                "parenttype": "Item",
                                "parent": ("in", frappe.get_all(
                                    "Item",
                                    filters={
                                        "variant_of": meta["item_code"],
                                        "disabled": 0,
                                    },
                                    pluck="name",
                                ) or ["__none__"]),
                            },
                            fields=["attribute", "attribute_value"],
                        )
                        option_ids = []
                        seen_opts = set()
                        for r in variant_attr_rows:
                            a = (r.get("attribute") or "").strip()
                            v = (r.get("attribute_value") or "").strip()
                            if not a or not v:
                                continue
                            oid = _opt_uuid(a, v, kind="variant")
                            if oid not in seen_opts:
                                seen_opts.add(oid)
                                option_ids.append(oid)
                        if option_ids:
                            try:
                                cfg_pusher(new_external_id, option_ids)
                            except Exception as exc:  # noqa: BLE001
                                result.errors.append(
                                    f"{meta['item_code']}: template configurator push failed: {exc}"
                                )
                    else:
                        # Legacy path: walk variant docs and call the
                        # adapter helper to resolve option ids from
                        # Shopware. Used when the bulk-coalesce
                        # pipeline isn't available (e.g. tests, mock
                        # adapters).
                        variant_rows = frappe.get_all(
                            "Item",
                            filters={"variant_of": meta["item_code"], "disabled": 0},
                            pluck="name",
                        )
                        option_ids: list[str] = []
                        seen_opts: set[str] = set()
                        for vcode in variant_rows:
                            vext = frappe.db.get_value(
                                _ECOMMERCE_ITEM_DOCTYPE,
                                {"erpnext_item_code": vcode, "integration": integration_key},
                                "integration_item_code",
                            )
                            if not vext:
                                continue
                            try:
                                v_doc = frappe.get_doc("Item", vcode)
                            except frappe.DoesNotExistError:
                                continue
                            av = [
                                {"name": a.attribute, "value": a.attribute_value}
                                for a in (v_doc.attributes or [])
                                if a.attribute and a.attribute_value
                            ]
                            if not av:
                                continue
                            try:
                                res = opt_pusher(vext, av) or {}
                            except Exception:  # noqa: BLE001
                                continue
                            for oid in res.get("option_ids") or []:
                                if oid not in seen_opts:
                                    seen_opts.add(oid)
                                    option_ids.append(oid)
                        if option_ids:
                            try:
                                cfg_pusher(new_external_id, option_ids)
                            except Exception as exc:  # noqa: BLE001
                                result.errors.append(
                                    f"{meta['item_code']}: template configurator push failed: {exc}"
                                )

        applied_diffs.append({
            "item_code": meta["item_code"],
            "action": meta["action"],
            "status": "ok",
            "external_id": new_external_id,
        })
        if meta["action"] == ACTION_CREATE:
            result.created += 1
        else:
            result.updated += 1

    # Close the shared Phase-C Shopware session (no-op when backend
    # is not Shopware). Done in a finally-like guard so a per-item
    # exception above can't leak the session.
    if _phase_c_session is not None:
        try:
            _phase_c_session.__exit__(None, None, None)
        except Exception:  # noqa: BLE001
            pass


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
    """Push one item to the backend, persist mapping + hash on success.

    Retained for callers (tests, subset runs) that want one-at-a-time
    semantics. The main apply loop now uses :func:`_apply_batch` for
    throughput.
    """
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
        ["name", "integration_item_code", "variant_id"],
        as_dict=True,
    )
    external_id = ecom_row.get("integration_item_code") if ecom_row else None
    variant_id = ecom_row.get("variant_id") if ecom_row else None

    if sync_doc.backend == BACKEND_SHOPWARE:
        payload = build_shopware_payload(item, sync_doc, canonical, external_id=external_id)
    elif sync_doc.backend == BACKEND_MEDUSA:
        payload = build_medusa_payload(
            item, sync_doc, canonical,
            external_id=external_id, variant_id=variant_id,
        )
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


def _ensure_brands_for_run(*, sync_doc, plan, adapter, run_name: str) -> None:
    """Push every distinct brand in this run's plan to Shopware ONCE
    per ``_apply_live`` call, with per-brand delta against
    ``Brand.last_synced_brand_hash``.

    Why once-per-run, not once-per-batch: brand entities (logo +
    description + link) don't vary across batches of the same run,
    and the logo URL-fetch is the most expensive step. Looping it
    1500× for a 37k-item catalogue was pure waste.

    Why per-brand delta: a cron tick on an unchanged catalogue
    must produce ZERO backend writes (per the delta-sync contract
    in CLAUDE.md). Hashing the brand's source fields lets us
    skip the upsert when nothing changed since the last successful
    push.
    """
    if not hasattr(adapter, "ensure_brand_entities_bulk"):
        return
    # Collect distinct brand names in scope (creates + updates).
    item_codes: set[str] = set()
    for node in (plan.creates or []):
        item_codes.add(node.item_code)
    for node in (plan.updates or []):
        item_codes.add(node.item_code)
    if not item_codes:
        return
    brand_names = frappe.db.sql_list("""
      SELECT DISTINCT brand FROM `tabItem`
      WHERE name IN %(codes)s AND brand IS NOT NULL AND brand != ''
    """, {"codes": tuple(item_codes) or ("__none__",)})
    if not brand_names:
        return
    from ecommerce_integrations.product_sync.engine.payload import (
        _resolve_image_public_base,
    )
    base = _resolve_image_public_base()
    rows = frappe.get_all(
        "Brand",
        filters={"name": ("in", sorted(brand_names))},
        fields=["name", "image", "description"],
    )
    # Per-brand delta check: hash (image, description) and compare
    # against last-synced hash on the Brand doc. Field is added via
    # the patch alongside this code; skip the hash check (and push
    # everything) when the field isn't installed yet so first
    # rollout works without a patch step.
    has_hash_field = bool(frappe.db.has_column("Brand", "last_synced_brand_hash"))
    drifted: list[dict] = []
    drifted_hashes: dict[str, str] = {}
    for r in rows:
        img_raw = (r.get("image") or "").strip()
        if img_raw and not img_raw.startswith(("http://", "https://")) and base:
            img_abs = base.rstrip("/") + (
                img_raw if img_raw.startswith("/") else "/" + img_raw
            )
        else:
            img_abs = img_raw
        desc = (r.get("description") or "").strip()
        import hashlib as _hl
        h = _hl.sha256(
            f"img={img_abs}\ndesc={desc}".encode("utf-8")
        ).hexdigest()
        if has_hash_field:
            stored = frappe.db.get_value("Brand", r["name"], "last_synced_brand_hash") or ""
            if stored == h:
                continue
        drifted.append({
            "name": r["name"],
            "description": desc,
            "logo_url": img_abs,
        })
        drifted_hashes[r["name"]] = h
    if not drifted:
        _logger().info(
            f"brand pre-flight: no drift, skipped ({len(rows)} brands in scope)"
        )
        return
    _logger().info(
        f"brand pre-flight: pushing {len(drifted)} of {len(rows)} brands "
        f"(drifted on logo/description)"
    )
    adapter.ensure_brand_entities_bulk(drifted)
    if has_hash_field:
        for name, h in drifted_hashes.items():
            try:
                frappe.db.set_value(
                    "Brand", name, "last_synced_brand_hash", h,
                    update_modified=False,
                )
            except Exception:  # noqa: BLE001
                pass
        frappe.db.commit()


def _persist_item_success(
    *,
    item_code: str,
    integration: str,
    external_id: str,
    proposed_hash: str,
    run_name: str,
    existing_row: dict | None,
    canonical: dict | None = None,
) -> None:
    """Upsert tabEcommerce Item with the new external_id + hash.

    When ``canonical`` is supplied the full payload is gzip-base64
    encoded and stored on ``last_synced_canonical`` so the next
    differ can compute per-field changes instead of just hash
    equality. Callers that don't yet have the canonical (older code
    paths, partial recovery flows) can omit it — the field stays
    ``NULL`` and the next push falls back to full-payload behaviour.
    """
    from ecommerce_integrations.product_sync.engine.canonical import (
        encode_canonical,
    )
    now = now_datetime()
    encoded_canonical = encode_canonical(canonical) if canonical else None
    fields = {
        "integration_item_code": external_id,
        "last_synced_hash": proposed_hash,
        "last_synced_at": now,
        "last_sync_run": run_name,
    }
    if encoded_canonical is not None:
        fields["last_synced_canonical"] = encoded_canonical
    if existing_row:
        frappe.db.set_value(
            _ECOMMERCE_ITEM_DOCTYPE,
            existing_row["name"],
            fields,
            update_modified=False,
        )
    else:
        doc_values = {
            "doctype": _ECOMMERCE_ITEM_DOCTYPE,
            "erpnext_item_code": item_code,
            "integration": integration,
            "sku": item_code,
            **fields,
        }
        doc = frappe.get_doc(doc_values)
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
    frappe.db.commit()  # make the audit row visible while the worker is still running
    return doc


def _finalize_run(
    run, result: ProductSyncRunResult, *,
    status: str, message: str | None,
    applied_diffs: list[dict],
    started_ts: float,
    ai_generated: int = 0,
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
            "ai_descriptions_generated": ai_generated,
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


# ─── AI pre-pass ─────────────────────────────────────────────────────


def _ai_pre_pass(sync_doc, *, subset_item_codes: list[str] | None) -> int:
    """Generate AI descriptions for in-scope Items that don't have one.

    Walks the Sync's scope, picks items where ``ai_description_generated``
    is falsy, hands a bounded batch to ``ai_description.gemini.
    generate_descriptions_batch``. Returns the count of successful
    generations. All failures are logged via the AI module's own
    audit log (``AI Description Log``) — this function only reports
    the headline number for the Sync Run audit row.

    The batch ceiling comes from ``sync_doc.max_ai_pre_generate``;
    setting it to 0 means "unbounded" (operator override for one-off
    backfills). Default 100 keeps a single run from torching the
    token budget on the first invocation of a 30k-item catalogue.
    """
    try:
        from ecommerce_integrations.ai_description.gemini import (
            generate_descriptions_batch,
        )
        from ecommerce_integrations.product_sync.walker import (
            walk_items_for_sync,
        )
    except Exception:  # noqa: BLE001 — AI module is optional
        return 0

    max_gen_raw = getattr(sync_doc, "max_ai_pre_generate", 100)
    try:
        max_gen = int(max_gen_raw or 0)
    except (TypeError, ValueError):
        max_gen = 100
    if max_gen < 0:
        max_gen = 0

    candidates: list[str] = []
    subset_set = set(subset_item_codes or [])
    for node in walk_items_for_sync(sync_doc):
        if subset_set and node.item_code not in subset_set:
            continue
        already = frappe.db.get_value(
            "Item", node.item_code, "ai_description_generated",
        )
        if already:
            continue
        candidates.append(node.item_code)
        if max_gen and len(candidates) >= max_gen:
            break

    if not candidates:
        return 0

    try:
        result = generate_descriptions_batch(candidates)
    except Exception as exc:  # noqa: BLE001
        frappe.log_error(
            title=f"AI pre-pass failed for Sync {sync_doc.name}",
            message=str(exc),
        )
        return 0

    return int((result or {}).get("success", 0) or 0)


# ─── Stale-run recovery ──────────────────────────────────────────────
# Mirrors ``catalog_mirror.tasks.recover_stale_mirrors`` /
# ``smart_collections.tasks.recover_stale_targets``. Wired into the
# hourly scheduler so a worker killed mid-apply (OOM, SIGKILL, container
# restart) doesn't leave the parent Sync row flagged ``running``
# forever — ``claim_sync`` refuses every retrigger until the claim is
# released.


def recover_stale_product_syncs() -> dict:
    """Sweep Product Sync rows + Run rows stuck in ``running``.

    A run is considered stale when its ``modified`` (the heartbeat
    timestamp from ``_heartbeat``) is older than
    ``_HEARTBEAT_TIMEOUT_MIN``. Marks the run as error+finished and
    releases the parent sync claim so the next trigger can fire.
    """
    from frappe.utils import add_to_date

    if frappe.flags.in_install or frappe.flags.in_migrate:
        return {"recovered": 0}
    if not frappe.db.exists("DocType", _RUN_DOCTYPE):
        return {"recovered": 0}

    cutoff = add_to_date(now_datetime(), minutes=-_HEARTBEAT_TIMEOUT_MIN)

    stale_runs = frappe.get_all(
        _RUN_DOCTYPE,
        filters={
            "status": "running",
            "modified": ["<", cutoff],
        },
        fields=["name", "sync"],
    )

    recovered_runs = 0
    released_syncs: set[str] = set()
    for r in stale_runs:
        frappe.db.set_value(
            _RUN_DOCTYPE, r.name,
            {
                "status": STATUS_ERROR,
                "finished_at": now_datetime(),
                "error_summary": _("heartbeat timeout — worker likely killed (OOM / SIGKILL / container restart)"),
            },
            update_modified=False,
        )
        recovered_runs += 1
        if r.sync and r.sync not in released_syncs:
            # Only release the parent if it's still flagged 'running'
            # AND points at this stale run (don't accidentally release
            # a newer concurrent run that just started).
            current = frappe.db.get_value(
                SYNC_DOCTYPE, r.sync,
                ["sync_status", "last_heartbeat_at"],
                as_dict=True,
            )
            if (
                current
                and (current.sync_status or "").lower() == "running"
                and current.last_heartbeat_at
                and current.last_heartbeat_at < cutoff
            ):
                _release_sync(
                    r.sync, status=STATUS_ERROR,
                    last_error=_("heartbeat timeout — stale run recovered"),
                )
                released_syncs.add(r.sync)

    frappe.db.commit()
    if recovered_runs:
        _logger().info(
            f"recover_stale_product_syncs: recovered {recovered_runs} run(s) / "
            f"released {len(released_syncs)} parent sync(s)"
        )
    return {"recovered_runs": recovered_runs, "released_syncs": list(released_syncs)}
