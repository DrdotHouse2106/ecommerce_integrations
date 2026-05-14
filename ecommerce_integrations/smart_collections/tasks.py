"""Sync orchestration for Smart Collections.

``sync_collection`` and ``sync_all_collections`` are the entry points the
form button, the scheduled job and the Item-change hooks all converge
on. Per (collection, target) the flow is::

    items   = resolver.resolve(collection)
    diff    = differ.compute_diff(collection, target_idx, items)
    ext_id  = adapter.upsert_category(collection, target)
    missing = adapter.link_items(target, diff.to_add)
              adapter.unlink_items(target, diff.to_remove)
    differ.save_snapshot(collection, target_idx, items)
    target.{sync_status, last_synced_at, last_error, external_id} updated

Errors on individual targets do not abort other targets — each is
caught, persisted on ``target.sync_status`` and ``last_error``, and
mirrored into ``Ecommerce Integration Log`` for the full traceback.

The scheduled hook ``sync_due_collections`` is the periodic driver; the
``sync_collection_now`` whitelisted endpoint backs the form button.

Dry-run path
------------
When called with ``dry_run=True``, :func:`sync_collection` returns a
:class:`~ecommerce_integrations.smart_collections.engine.preview.CollectionSyncPlan`
*without performing any writes* — no adapter mutations, no snapshot
updates, no ``sync_status`` change. The plan diffs the resolved set
against the **live backend state** (via ``adapter.fetch_state``) plus
the local snapshot, so the form dialog can show both "what we would
push" and "what drifted in the backend since the last successful
sync".

Heartbeat / running state
-------------------------
A live ``_sync_target`` call flips the target's ``sync_status`` to
``running`` and writes ``last_heartbeat_at`` before any adapter HTTP
calls. :func:`recover_stale_targets` (registered on the hourly
scheduler) sweeps any target still in ``running`` past the heartbeat
timeout and flags it as ``error`` so a crashed worker doesn't strand
the row forever.
"""

import frappe
from frappe.utils import add_to_date, now_datetime

from ecommerce_integrations.smart_collections.channel_visibility import (
    invalidate_cache,
)
from ecommerce_integrations.smart_collections.engine.adapters.base import (
    AdapterError,
    LiveCategoryState,
    get_adapter,
)
from ecommerce_integrations.smart_collections.engine.differ import (
    compute_diff,
    load_snapshot,
    save_snapshot,
)
from ecommerce_integrations.smart_collections.engine.preview import (
    CollectionSyncPlan,
    TargetSyncPlan,
)
from ecommerce_integrations.smart_collections.engine.resolver import resolve


_LOG_DOCTYPE = "Ecommerce Integration Log"
_COLLECTION_DOCTYPE = "Ecommerce Smart Collection"
_TARGET_DOCTYPE = "Ecommerce Smart Collection Target"

# A live ``_sync_target`` call that runs past this window without
# completing is considered stuck. :func:`recover_stale_targets`
# re-flags it as ``error``. 30 minutes is well above the slowest
# observed bulk-link batch and below the hourly scheduler cadence,
# so a stuck target is never left ``running`` across two scheduled
# runs in a row.
_HEARTBEAT_TIMEOUT_MIN = 30


@frappe.whitelist()
def sync_collection_now(collection: str) -> dict:
    """Whitelisted entry — used by the form button on a Smart Collection."""
    if not frappe.has_permission(_COLLECTION_DOCTYPE, "write", doc=collection):
        frappe.throw("Not permitted to sync this collection")
    return sync_collection(collection)


def sync_collection(collection_name: str, *, dry_run: bool = False):
    """Sync one collection across all its enabled targets.

    Returns a summary dict on the live path. When ``dry_run=True``
    returns a :class:`CollectionSyncPlan` describing what the live path
    *would* do, without persisting anything.
    """
    coll = frappe.get_doc(_COLLECTION_DOCTYPE, collection_name)

    if dry_run:
        return _build_preview(coll)

    if not coll.is_active:
        return {
            "collection": collection_name,
            "skipped": True,
            "reason": "collection inactive",
            "targets": [],
        }

    items = resolve(coll)

    if not items and not coll.allow_empty:
        return {
            "collection": collection_name,
            "skipped": True,
            "reason": "empty result and allow_empty=0",
            "targets": [],
        }

    results = []
    invalidated_backends: set[str] = set()
    for target in coll.targets or []:
        if not target.enabled:
            continue
        results.append(_sync_target(coll, target, items))
        invalidated_backends.add(target.backend)

    for backend in invalidated_backends:
        invalidate_cache(backend)

    return {"collection": collection_name, "skipped": False, "targets": results}


def sync_all_collections(backend: str | None = None) -> dict:
    """Iterate every active collection. Optional ``backend`` filter is
    enforced via the join below so collections without any target on the
    chosen backend are skipped without loading them."""
    filters = "WHERE sc.is_active = 1"
    args: tuple = ()
    if backend:
        filters += (
            " AND EXISTS (SELECT 1 FROM `tabEcommerce Smart Collection Target` t"
            " WHERE t.parent = sc.name AND t.backend = %s AND t.enabled = 1)"
        )
        args = (backend,)

    rows = frappe.db.sql(
        f"SELECT sc.name FROM `tabEcommerce Smart Collection` sc {filters} "
        "ORDER BY sc.sort_order ASC, sc.title ASC",
        args,
        as_dict=True,
    )

    summary = {"total": len(rows), "ok": 0, "skipped": 0, "error": 0}
    for r in rows:
        try:
            outcome = sync_collection(r.name)
            if outcome.get("skipped"):
                summary["skipped"] += 1
                continue
            had_error = any(
                t.get("status") == "error" for t in outcome.get("targets") or []
            )
            if had_error:
                summary["error"] += 1
            else:
                summary["ok"] += 1
        except Exception as e:
            summary["error"] += 1
            frappe.log_error(
                title=f"Smart Collection sync failed: {r.name}", message=str(e)
            )
    return summary


def sync_due_collections() -> None:
    """Scheduled-job entry. Runs every active collection on every backend."""
    if frappe.flags.in_install or frappe.flags.in_migrate:
        return
    sync_all_collections()


def recover_stale_targets() -> dict:
    """Sweep targets stuck in ``running`` past the heartbeat timeout.

    Hourly scheduler entry. A worker that crashed mid-sync leaves
    ``sync_status='running'`` and ``last_heartbeat_at`` behind; without
    this sweep the next form load would show the row as still in
    progress forever. We flip it to ``error`` so the operator sees
    something actionable.

    Returns a summary dict for observability — the scheduler ignores
    the return value but the same function is callable manually from
    ``bench --site … console``.
    """
    if frappe.flags.in_install or frappe.flags.in_migrate:
        return {"recovered": 0}
    if not frappe.db.exists("DocType", _TARGET_DOCTYPE):
        return {"recovered": 0}

    cutoff = add_to_date(now_datetime(), minutes=-_HEARTBEAT_TIMEOUT_MIN)
    stale = frappe.get_all(
        _TARGET_DOCTYPE,
        filters={
            "sync_status": "running",
            "last_heartbeat_at": ["<", cutoff],
        },
        pluck="name",
    )
    for name in stale:
        frappe.db.set_value(
            _TARGET_DOCTYPE, name,
            {
                "sync_status": "error",
                "last_error": "heartbeat timeout",
            },
            update_modified=False,
        )
    frappe.db.commit()  # noqa: SLF001 — release the recovered rows so the next sync can claim them
    return {"recovered": len(stale)}


def _sync_target(coll, target, items: set[str]) -> dict:
    # Heartbeat: claim the row before any HTTP call so a crashed worker
    # is visible to recover_stale_targets within the heartbeat window.
    # Commit immediately — the row needs to be observable from a
    # separate process (the scheduler) for that sweep to work.
    now = now_datetime()
    frappe.db.set_value(
        _TARGET_DOCTYPE, target.name,
        {
            "sync_status": "running",
            "last_heartbeat_at": now,
            "last_error": "",
        },
        update_modified=False,
    )
    frappe.db.commit()  # noqa: SLF001 — heartbeat must be visible cross-process

    log_name = _start_log(coll, target)
    try:
        adapter = get_adapter(target.backend)

        external_id = adapter.upsert_category(coll, target)
        if external_id and target.external_id != external_id:
            target.external_id = external_id

        diff = compute_diff(coll.name, target.idx, items)

        # ``link_only`` is the adopt-and-protect mode — when set, the
        # sync only ADDS items to the backend category and never
        # removes anything. Used right after an Adopt flow when the
        # operator wants to merge ERPNext-resolved items into a
        # manually-curated backend category without nuking the manual
        # picks. ``to_remove`` is still computed (and logged) so the
        # operator can see what *would* have been unlinked.
        link_only = int(getattr(target, "link_only", 0) or 0)
        unresolved = adapter.link_items(target, sorted(diff["to_add"])) or []
        removed_count = 0
        if not link_only:
            adapter.unlink_items(target, sorted(diff["to_remove"]))
            removed_count = len(diff["to_remove"])

        # Snapshot only items the adapter actually managed — items that
        # haven't been pushed to the backend yet should not be considered
        # "linked" for the next diff.
        unresolved_set = set(unresolved)
        synced_items = {i for i in items if i not in unresolved_set}
        save_snapshot(coll.name, target.idx, synced_items)

        now = now_datetime()
        frappe.db.set_value(
            _TARGET_DOCTYPE, target.name,
            {
                "external_id": target.external_id or "",
                "sync_status": "ok",
                "last_synced_at": now,
                "last_heartbeat_at": now,
                "last_error": "",
            },
            update_modified=False,
        )
        # Commit the final state before the next target starts so a
        # crash mid-run can't leave an "ok" target with stale linked
        # rows (Reliability finding R14).
        frappe.db.commit()  # noqa: SLF001 — per-target finalisation

        _finish_log(
            log_name,
            status="Success",
            message={
                "to_add": len(diff["to_add"]),
                "to_remove": len(diff["to_remove"]),
                "to_keep": len(diff["to_keep"]),
                "unresolved": len(unresolved),
                "link_only": link_only,
            },
        )
        return {
            "target_idx": target.idx,
            "backend": target.backend,
            "sales_channel": target.sales_channel,
            "status": "ok",
            "added": len(diff["to_add"]) - len(unresolved),
            "removed": removed_count,
            "to_remove_skipped": link_only and len(diff["to_remove"]) > 0,
            "unresolved": len(unresolved),
        }

    except AdapterError as e:
        return _record_failure(coll, target, log_name, e, str(e))
    except Exception as e:
        return _record_failure(
            coll, target, log_name, e, f"unexpected error: {type(e).__name__}"
        )


def _record_failure(coll, target, log_name, exc, short_msg: str) -> dict:
    now = now_datetime()
    frappe.db.set_value(
        _TARGET_DOCTYPE, target.name,
        {
            "sync_status": "error",
            "last_error": short_msg[:500],
            "last_synced_at": now,
            "last_heartbeat_at": now,
        },
        update_modified=False,
    )
    frappe.db.commit()  # noqa: SLF001 — persist failure before next target
    _finish_log(log_name, status="Error", error=str(exc))
    return {
        "target_idx": target.idx,
        "backend": target.backend,
        "sales_channel": target.sales_channel,
        "status": "error",
        "error": short_msg,
    }


def _start_log(coll, target) -> str | None:
    if not frappe.db.exists("DocType", _LOG_DOCTYPE):
        return None
    log = frappe.new_doc(_LOG_DOCTYPE)
    log.integration = (target.backend or "").lower() or "smart_collections"
    log.method = (
        f"smart_collections.tasks.sync_collection({coll.name}, target={target.idx})"
    )
    log.title = f"Sync {coll.title} -> {target.backend}/{target.sales_channel}"
    log.status = "Queued"
    log.flags.ignore_permissions = True
    log.insert()
    return log.name


def _finish_log(
    log_name: str | None, *, status: str, message=None, error: str | None = None
) -> None:
    if not log_name:
        return
    update: dict = {"status": status}
    if message is not None:
        import json

        update["response_data"] = json.dumps(message)
    if error is not None:
        update["traceback"] = error
    frappe.db.set_value(_LOG_DOCTYPE, log_name, update, update_modified=False)


# --------------------------------------------------------------------- #
# Dry-run preview
# --------------------------------------------------------------------- #


def _build_preview(coll) -> CollectionSyncPlan:
    """Build a :class:`CollectionSyncPlan` for ``coll`` without mutating anything.

    The plan compares the resolved item set against:

    1. The local snapshot (``compute_diff`` against the saved
       ``Ecommerce Smart Collection Snapshot``).
    2. The **live backend state** (``adapter.fetch_state``) — drift
       detection that flags storefront-side hand-edits before the
       operator triggers a sync that would overwrite them.
    """
    if not coll.is_active:
        return CollectionSyncPlan(
            collection=coll.name,
            title=coll.title or coll.name,
            is_active=int(coll.is_active or 0),
            skipped=True,
            skip_reason="collection inactive",
        )

    # Don't persist the resolver stats during a preview — the operator
    # is just looking at what would happen and shouldn't see
    # ``last_resolved_count`` jump on every click.
    items = resolve(coll, persist_stats=False)

    plan = CollectionSyncPlan(
        collection=coll.name,
        title=coll.title or coll.name,
        is_active=int(coll.is_active or 0),
        skipped=False,
        resolved_count=len(items),
        sample_items=_sample_items(items, limit=50),
    )

    if not items and not coll.allow_empty:
        plan.skipped = True
        plan.skip_reason = "empty result and allow_empty=0"
        return plan

    sorted_items = sorted(items)
    for target in coll.targets or []:
        if not int(target.enabled or 0):
            continue
        plan.targets.append(_build_target_plan(coll, target, sorted_items))

    return plan


def _sample_items(items: set[str], *, limit: int = 50) -> list[dict]:
    if not items:
        return []
    head = sorted(items)[:limit]
    rows = frappe.db.sql(
        f"""SELECT name, item_name, item_group
            FROM `tabItem`
            WHERE name IN ({", ".join(["%s"] * len(head))})""",
        tuple(head),
        as_dict=True,
    )
    by_name = {r.name: r for r in rows}
    return [
        {
            "name": code,
            "item_name": (by_name.get(code) or {}).get("item_name") or "",
            "item_group": (by_name.get(code) or {}).get("item_group") or "",
        }
        for code in head
    ]


def _build_target_plan(coll, target, sorted_items: list[str]) -> TargetSyncPlan:
    backend = target.backend
    proposed_name = coll.title or coll.name
    proposed_description = coll.description or ""
    proposed_active = int(coll.is_active or 0)
    link_only = int(getattr(target, "link_only", 0) or 0)

    # Resolve item codes -> backend product ids for both the
    # add-candidate set and the unresolved set. This duplicates the
    # mapping the live path does inside the adapter so the preview can
    # show "X items would be linked, Y are not yet exported".
    resolved_product_ids, unresolved_codes = _resolve_product_ids(
        backend, sorted_items,
    )

    # Live backend probe — wrapped in try/except so a transient network
    # blip on one adapter never blocks the preview for the others.
    live: LiveCategoryState
    live_error: str | None = None
    try:
        adapter = get_adapter(backend)
        live = adapter.fetch_state(target)
    except Exception as e:
        live = LiveCategoryState(exists=False)
        live_error = f"fetch_state failed: {e!s}"

    # Match suggestions only when the target hasn't been wired up yet.
    matches: list[dict] = []
    if not target.external_id:
        try:
            adapter = get_adapter(backend)
            for m in adapter.find_matching_category(coll, target) or []:
                matches.append(
                    {
                        "external_id": m.external_id,
                        "name": m.name,
                        "path": m.path,
                        "has_target_sales_channel": bool(
                            m.has_target_sales_channel,
                        ),
                        "linked_product_count": int(m.linked_product_count or 0),
                    },
                )
        except Exception as e:
            live_error = (
                f"{live_error}; find_matching_category failed: {e!s}"
                if live_error
                else f"find_matching_category failed: {e!s}"
            )

    # Local snapshot diff — what the live path's ``compute_diff``
    # would say if we ran it right now. ``to_add`` / ``to_remove`` /
    # ``to_keep`` are computed against the saved snapshot (or empty
    # on first run, in which case everything is ``to_add``).
    item_set = set(sorted_items)
    snap = load_snapshot(coll.name, target.idx)
    to_add = sorted(item_set - snap)
    to_remove = sorted(snap - item_set)
    to_keep = sorted(item_set & snap)

    # Live drift — items in the local snapshot but missing from the
    # backend's category right now (or the reverse). The orchestrator
    # itself silently overwrites this on a live sync; the preview
    # surfaces it as a warning before the operator clicks.
    drift_local_only: list[str] = []
    drift_backend_only_count = 0
    if live.exists:
        # ``linked_product_ids`` is on the backend; ``resolved_product_ids``
        # is what *would* be linked after this sync. Drift = backend has
        # items we don't track and vice versa.
        local_expected = set(resolved_product_ids)
        drift_local_only = sorted(local_expected - live.linked_product_ids)[:100]
        drift_backend_only_count = max(
            0, len(live.linked_product_ids - local_expected),
        )

    # Action: ``create`` when no live category exists (either no
    # external_id or stale id), ``update`` when something differs,
    # ``noop`` otherwise.
    action = _classify_action(coll, target, live, link_only)

    # Shopware sales-channel diff — Medusa always reports empty.
    sc_to_add: list[str] = []
    sc_to_remove: list[str] = []
    if backend == "Shopware" and target.sales_channel:
        # The adapter assigns exactly one sales channel per target.
        live_set = set(live.sales_channel_ids or [])
        if target.sales_channel not in live_set and live.exists:
            sc_to_add.append(target.sales_channel)

    notes: list[str] = []
    if live_error:
        notes.append(live_error)
    if not live.exists and target.external_id:
        notes.append("stale external_id — backend category not found")
    if link_only and to_remove:
        notes.append("link_only=1 — to_remove will be SKIPPED on the live sync")

    return TargetSyncPlan(
        target_idx=int(target.idx or 0),
        target_name=target.name,
        backend=backend,
        sales_channel=target.sales_channel or "",
        enabled=int(target.enabled or 0),
        link_only=link_only,
        action=action,
        external_id=target.external_id or None,
        live_exists=bool(live.exists),
        proposed_name=proposed_name,
        proposed_description=proposed_description,
        proposed_active=proposed_active,
        live_name=live.name,
        live_description=live.description,
        live_active=int(live.active) if live.exists else None,
        live_sales_channel_ids=list(live.sales_channel_ids or []),
        to_add=to_add,
        to_remove=to_remove,
        to_keep=to_keep,
        unresolved_items=list(unresolved_codes),
        drift_local_only=drift_local_only,
        drift_backend_only_count=drift_backend_only_count,
        match_suggestions=matches,
        to_remove_skipped=bool(link_only and to_remove),
        sales_channels_to_add=sc_to_add,
        sales_channels_to_remove=sc_to_remove,
        notes=notes,
    )


def _classify_action(coll, target, live: LiveCategoryState, link_only: int) -> str:
    if not live.exists:
        return "create"
    proposed_name = coll.title or coll.name
    proposed_description = coll.description or ""
    proposed_active = bool(coll.is_active)

    name_changed = (live.name or "") != proposed_name
    desc_changed = (live.description or "") != proposed_description
    active_changed = bool(live.active) != proposed_active

    if name_changed or desc_changed or active_changed:
        return "update"

    if target.backend == "Shopware" and target.sales_channel:
        if target.sales_channel not in (live.sales_channel_ids or []):
            return "update"

    return "noop"


def _resolve_product_ids(
    backend: str, item_codes: list[str],
) -> tuple[set[str], list[str]]:
    """Map ERPNext item codes to backend product ids for preview drift.

    Routes to the adapter-private resolver functions
    (``_items_to_shopware_product_ids`` /
    ``_items_to_medusa_product_ids``) — those already encode the
    canonical mapping rule via ``tabEcommerce Item`` (Wave 1 migrated
    Medusa onto the same table). Importing them here keeps the dry-run
    path consistent with the live path's resolution semantics.
    """
    if not item_codes:
        return set(), []

    if backend == "Shopware":
        from ecommerce_integrations.smart_collections.engine.adapters.shopware import (
            _items_to_shopware_product_ids,
        )
        ids, missing = _items_to_shopware_product_ids(item_codes)
        return set(ids), list(missing)

    if backend == "Medusa":
        from ecommerce_integrations.smart_collections.engine.adapters.medusa import (
            _items_to_medusa_product_ids,
        )
        ids, missing = _items_to_medusa_product_ids(item_codes)
        return set(ids), list(missing)

    return set(), list(item_codes)
