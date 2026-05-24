"""Sync orchestrator for Catalog Mirror.

``apply_mirror`` is the single entry point — dry-run returns a
:class:`MirrorPreviewPlan` with no side effects; live runs apply the
diff via the registered adapter and persist ``<backend>_category_id``
back onto each Item Group.

Heartbeat / running state mirrors the Smart Collections orchestrator:
``sync_status='running'`` and ``last_heartbeat_at=now()`` are committed
before any HTTP call so :func:`recover_stale_mirrors` (hourly scheduler
entry) can sweep workers that died mid-sync.

Scheduler integration (sync_due_mirrors / recover_stale_mirrors) is
documented here but *not* wired into ``hooks.py``; that wiring lands in
Phase 6 once the live-apply path has soaked in.
"""

from __future__ import annotations

from collections.abc import Iterable

import frappe
from frappe import _
from frappe.utils import add_to_date, now_datetime

from ecommerce_integrations.catalog_mirror.constants import (
    ACTION_CREATE,
    ACTION_MOVE,
    ACTION_UPDATE,
    BACKEND_MEDUSA,
    BACKEND_SHOPWARE,
    ORPHAN_DELETE,
)
from ecommerce_integrations.catalog_mirror.differ import (
    TreeDiff,
    compute_tree_diff,
)
from ecommerce_integrations.catalog_mirror.engine.adapters.base import (
    AdapterError,
    CatalogAdapter,
    get_adapter,
)
from ecommerce_integrations.catalog_mirror.engine.preview import (
    MirrorPreviewPlan,
    MirrorRunResult,
    NodePlan,
)
from ecommerce_integrations.catalog_mirror.walker import (
    ErpTreeNode,
    walk_erpnext_tree,
)

_MIRROR_DOCTYPE = "Ecommerce Catalog Mirror"
_LOG_DOCTYPE = "Ecommerce Integration Log"
_HEARTBEAT_TIMEOUT_MIN = 30


def apply_mirror(
    mirror_name: str, *, dry_run: bool = False,
) -> MirrorPreviewPlan | MirrorRunResult:
    """Compute (and optionally apply) the diff for one Catalog Mirror.

    ``dry_run=True`` returns a :class:`MirrorPreviewPlan` without
    touching the DB or the backend. ``dry_run=False`` claims the mirror
    (sync_status='running'), applies the diff via the registered
    adapter, persists ``<backend>_category_id`` mappings, and finalises
    sync_status='ok' or 'error'.
    """
    mirror = frappe.get_doc(_MIRROR_DOCTYPE, mirror_name)

    if dry_run:
        return _build_preview(mirror)

    if not int(mirror.is_active or 0):
        return MirrorRunResult(
            mirror=mirror_name,
            backend=mirror.backend or "",
            status="skipped",
            skip_reason="mirror inactive",
        )

    # Skip cleanly when the backend's master toggle is off. The session
    # decorators return None silently in that case which corrupts the
    # apply loop (it'd later try to use a None LiveCategoryNode and the
    # worker crashes between the running-flip and the error-flip). Skip
    # BEFORE claiming the row so the operator's UI doesn't blink to
    # running and back.
    if _is_backend_disabled(mirror.backend or ""):
        return MirrorRunResult(
            mirror=mirror_name,
            backend=mirror.backend or "",
            status="skipped",
            skip_reason=f"{mirror.backend} integration is disabled",
        )

    # Heartbeat: claim before any HTTP call so a crashed worker is
    # observable to recover_stale_mirrors within the heartbeat window.
    now = now_datetime()
    frappe.db.set_value(
        _MIRROR_DOCTYPE, mirror_name,
        {
            "sync_status": "running",
            "last_heartbeat_at": now,
            "last_error": "",
        },
        update_modified=False,
    )
    frappe.db.commit()

    log_name = _start_log(mirror)
    try:
        result = _apply_live(mirror)
        now = now_datetime()
        frappe.db.set_value(
            _MIRROR_DOCTYPE, mirror_name,
            {
                "sync_status": "ok",
                "last_synced_at": now,
                "last_heartbeat_at": now,
                "last_error": "",
            },
            update_modified=False,
        )
        frappe.db.commit()
        _finish_log(
            log_name,
            status="Success",
            message={
                "created": result.created,
                "updated": result.updated,
                "moved": result.moved,
                "deleted": result.deleted,
                "errors": result.errors,
            },
        )
        return result
    except Exception as e:
        msg = f"unexpected error: {type(e).__name__}: {e}"
        return _record_failure(mirror, log_name, e, msg)


def sync_due_mirrors() -> dict:
    """Scheduled-job entry — runs every active mirror.

    Intended for the hourly_long scheduler; *not* wired up in this phase
    (the wiring lands in Phase 6 once the apply path has soaked in).
    Safe to invoke manually from ``bench --site … console``.
    """
    if frappe.flags.in_install or frappe.flags.in_migrate:
        return {"total": 0, "ok": 0, "error": 0, "skipped": 0}
    if not frappe.db.exists("DocType", _MIRROR_DOCTYPE):
        return {"total": 0, "ok": 0, "error": 0, "skipped": 0}

    summary = {"total": 0, "ok": 0, "error": 0, "skipped": 0}
    names = frappe.get_all(
        _MIRROR_DOCTYPE,
        filters={"is_active": 1},
        pluck="name",
    )
    summary["total"] = len(names)
    for name in names:
        try:
            outcome = apply_mirror(name)
            if isinstance(outcome, MirrorRunResult):
                if outcome.status == "ok":
                    summary["ok"] += 1
                elif outcome.status == "skipped":
                    summary["skipped"] += 1
                else:
                    summary["error"] += 1
        except Exception as e:
            summary["error"] += 1
            # Defensive: apply_mirror is supposed to record its own
            # error state, but if it crashed before reaching that
            # finally-block we'd leave the mirror stranded at 'running'.
            # Force a terminal state so recover_stale_mirrors doesn't
            # have to wait out the heartbeat window.
            try:
                frappe.db.set_value(
                    _MIRROR_DOCTYPE, name,
                    {
                        "sync_status": "error",
                        "last_error": f"crashed in sync_due_mirrors: {type(e).__name__}: {e}"[:500],
                        "last_heartbeat_at": now_datetime(),
                    },
                    update_modified=False,
                )
                frappe.db.commit()
            except Exception:
                pass
            frappe.log_error(
                title=f"Catalog Mirror sync failed: {name}",
                message=frappe.get_traceback(),
            )
    return summary


def recover_stale_mirrors() -> dict:
    """Sweep mirrors stuck in ``running`` past the heartbeat timeout.

    Mirrors the Smart Collections recovery sweep. Same window
    (30 minutes), same flip-to-error behaviour. Intended for the
    hourly scheduler (wired in Phase 6).
    """
    if frappe.flags.in_install or frappe.flags.in_migrate:
        return {"recovered": 0}
    if not frappe.db.exists("DocType", _MIRROR_DOCTYPE):
        return {"recovered": 0}

    cutoff = add_to_date(now_datetime(), minutes=-_HEARTBEAT_TIMEOUT_MIN)
    stale = frappe.get_all(
        _MIRROR_DOCTYPE,
        filters={
            "sync_status": "running",
            "last_heartbeat_at": ["<", cutoff],
        },
        pluck="name",
    )
    for name in stale:
        frappe.db.set_value(
            _MIRROR_DOCTYPE, name,
            {
                "sync_status": "error",
                "last_error": _("heartbeat timeout"),
            },
            update_modified=False,
        )
    frappe.db.commit()
    return {"recovered": len(stale)}


# ----- internals --------------------------------------------------------


def _build_preview(mirror) -> MirrorPreviewPlan:
    # Dry-run previews are intentionally allowed when the mirror is
    # inactive — the operator needs to be able to inspect what *would*
    # happen before flipping the toggle. Apply paths still refuse to
    # mutate the live backend when is_active=0 (see ``apply_mirror``).
    # We only annotate the plan so the dialog can surface a hint.
    erp_tree = walk_erpnext_tree(
        mirror.root_item_group,
        include_inactive_leaves=bool(mirror.sync_inactive_leaves),
    )
    mapping = _load_mapping(mirror, erp_tree)

    diff, live_root_id, live_error = _fetch_diff(mirror, erp_tree, mapping)

    plan = MirrorPreviewPlan(
        mirror=mirror.name,
        title=mirror.title or mirror.name,
        backend=mirror.backend,
        target_sales_channel=mirror.target_sales_channel,
        root_item_group=mirror.root_item_group,
        external_root_id=live_root_id,
        is_active=int(mirror.is_active or 0),
        unresolved_external_root=bool(diff.unresolved_external_root),
        creates=list(diff.creates),
        updates=list(diff.updates),
        moves=list(diff.moves),
        noops=list(diff.noops),
        orphans=list(diff.orphans),
        mapping_drift=list(diff.mapping_drift),
    )
    if live_error:
        plan.notes.append(live_error)
    if not int(mirror.is_active or 0):
        plan.notes.append(
            _("Mirror is disabled — this is preview-only. "
              "Setze 'Aktiv' und speichere, bevor du 'Apply Live' drückst.")
        )
    return plan


def _apply_live(mirror) -> MirrorRunResult:
    erp_tree = walk_erpnext_tree(
        mirror.root_item_group,
        include_inactive_leaves=bool(mirror.sync_inactive_leaves),
    )
    mapping = _load_mapping(mirror, erp_tree)

    diff, live_root_id, _ = _fetch_diff(mirror, erp_tree, mapping)

    adapter = get_adapter(mirror.backend)
    result = MirrorRunResult(
        mirror=mirror.name,
        backend=mirror.backend,
        status="ok",
    )

    # The root itself: ensure ``external_root_id`` is pinned. If the
    # diff includes a create for the root IG (because no live root was
    # found) we create it via the adapter; otherwise we adopt the live
    # root id we already have.
    root_external_id = live_root_id
    root_create = _find_root_create(diff.creates, erp_tree.name)
    if root_create is not None:
        try:
            root_external_id = adapter.upsert_node(
                external_id=None,
                parent_external_id=root_create.proposed_parent_external_id,
                name=root_create.proposed_name,
                description=root_create.proposed_description,
                active=root_create.proposed_active,
                target_sales_channel=mirror.target_sales_channel,
            )
            result.created += 1
            mapping[erp_tree.name] = root_external_id
            _persist_mapping(mirror.backend, erp_tree.name, root_external_id)
        except AdapterError as e:
            result.errors.append(f"root create failed: {e}")
            result.status = "error"
            return result

    if root_external_id and (mirror.external_root_id or "") != root_external_id:
        frappe.db.set_value(
            _MIRROR_DOCTYPE, mirror.name,
            {"external_root_id": root_external_id},
            update_modified=False,
        )

    # Apply non-root creates parent-first (the differ emits them in
    # pre-order so a simple iteration preserves the invariant).
    #
    # ``plan.proposed_parent_external_id`` was resolved at differ-time:
    # if the parent IG was itself a pending CREATE in this same run,
    # the differ saw no mapping and emitted ``None``. Falling back to
    # ``root_external_id`` in that case silently re-parents the child
    # to the mirror root — without this guard, a fresh import where
    # several new sub-IGs land in the same run leaks their leaf-most
    # children directly under the mirror root instead of nesting them
    # under the just-created intermediate IGs. Re-resolve from the
    # in-flight ``mapping`` dict (populated as creates succeed below)
    # before falling back, and only fall back to root when the IG's
    # ERPNext parent is the mirror root itself.
    for plan in diff.creates:
        if plan.item_group == erp_tree.name:
            continue
        try:
            parent_ext = plan.proposed_parent_external_id
            if not parent_ext:
                parent_ig = frappe.db.get_value(
                    "Item Group", plan.item_group, "parent_item_group",
                )
                if parent_ig and parent_ig != erp_tree.name:
                    parent_ext = mapping.get(parent_ig)
                    if not parent_ext:
                        # Parent IG isn't mapped yet AND isn't the
                        # mirror root → would land under root by
                        # accident. Skip this create; the next mirror
                        # run will pick it up once the parent is mapped.
                        result.errors.append(
                            f"create {plan.item_group}: parent "
                            f"{parent_ig!r} not yet mapped; deferring"
                        )
                        continue
                else:
                    parent_ext = root_external_id
            ext_id = adapter.upsert_node(
                external_id=None,
                parent_external_id=parent_ext,
                name=plan.proposed_name,
                description=plan.proposed_description,
                active=plan.proposed_active,
                target_sales_channel=mirror.target_sales_channel,
            )
            mapping[plan.item_group] = ext_id
            _persist_mapping(mirror.backend, plan.item_group, ext_id)
            result.created += 1
        except AdapterError as e:
            result.errors.append(f"create {plan.item_group}: {e}")
            result.status = "error"

    for plan in diff.updates:
        try:
            adapter.upsert_node(
                external_id=plan.current_external_id,
                parent_external_id=plan.proposed_parent_external_id,
                name=plan.proposed_name,
                description=plan.proposed_description,
                active=plan.proposed_active,
                target_sales_channel=mirror.target_sales_channel,
            )
            result.updated += 1
        except AdapterError as e:
            result.errors.append(f"update {plan.item_group}: {e}")
            result.status = "error"

    # Moves bottom-up so a subtree re-parent applies as one logical
    # unit (the move on the parent carries its already-attached
    # descendants).
    for plan in sorted(diff.moves, key=_move_depth, reverse=True):
        try:
            adapter.move_node(
                plan.current_external_id,
                plan.proposed_parent_external_id,
            )
            result.moved += 1
        except AdapterError as e:
            result.errors.append(f"move {plan.item_group}: {e}")
            result.status = "error"

    # Mapping drift: clear stale ``<backend>_category_id`` so the next
    # apply re-creates fresh. The preview already reported this; the
    # live path just cleans up.
    for drift in diff.mapping_drift:
        _persist_mapping(mirror.backend, drift.item_group, "")
        mapping.pop(drift.item_group, None)

    if mirror.orphan_policy == ORPHAN_DELETE:
        for orphan in diff.orphans:
            try:
                adapter.delete_node(orphan.external_id)
                result.deleted += 1
            except AdapterError as e:
                result.errors.append(f"delete {orphan.name}: {e}")
                result.status = "error"

    return result


def _fetch_diff(
    mirror, erp_tree: ErpTreeNode, mapping: dict[str, str],
) -> tuple[TreeDiff, str | None, str | None]:
    """Resolve the live root, walk it, and compute the diff.

    Returns ``(diff, live_root_external_id, error_message)``. The
    error_message is operator-facing — surfaced as a preview note when
    set, never raised back to the caller.
    """
    adapter: CatalogAdapter | None
    try:
        adapter = get_adapter(mirror.backend)
    except AdapterError as e:
        # No adapter registered: produce an empty diff with the
        # ``unresolved_external_root`` flag so the preview can render a
        # warning. Live apply will fail at get_adapter() again.
        diff = TreeDiff(unresolved_external_root=True)
        return diff, None, str(e)

    live_root_id = mirror.external_root_id or None
    live_root_id = _resolve_external_root(mirror, live_root_id)

    if not live_root_id:
        diff = compute_tree_diff(
            erp_tree=erp_tree,
            live_tree=None,
            mapping=mapping,
            name_template=mirror.name_template or "{{ item_group.item_group_name }}",
            description_template=mirror.description_template or "",
            orphan_policy=mirror.orphan_policy or "keep",
        )
        return diff, None, None

    try:
        live_tree = adapter.fetch_tree(live_root_id)
    except (AdapterError, NotImplementedError) as e:
        diff = compute_tree_diff(
            erp_tree=erp_tree,
            live_tree=None,
            mapping=mapping,
            name_template=mirror.name_template or "{{ item_group.item_group_name }}",
            description_template=mirror.description_template or "",
            orphan_policy=mirror.orphan_policy or "keep",
        )
        return diff, live_root_id, f"fetch_tree failed: {e}"

    if live_tree is None:
        diff = compute_tree_diff(
            erp_tree=erp_tree,
            live_tree=None,
            mapping=mapping,
            name_template=mirror.name_template or "{{ item_group.item_group_name }}",
            description_template=mirror.description_template or "",
            orphan_policy=mirror.orphan_policy or "keep",
        )
        return diff, live_root_id, None

    # The mapping must include the root → live-root mapping so the
    # differ can resolve children's parent_external_id without a special
    # case for "the IG whose parent is the root".
    mapping_for_diff = dict(mapping)
    mapping_for_diff[erp_tree.name] = live_root_id

    diff = compute_tree_diff(
        erp_tree=erp_tree,
        live_tree=live_tree,
        mapping=mapping_for_diff,
        name_template=mirror.name_template or "{{ item_group.item_group_name }}",
        description_template=mirror.description_template or "",
        orphan_policy=mirror.orphan_policy or "keep",
    )
    return diff, live_root_id, None


def _resolve_external_root(mirror, recorded: str | None) -> str | None:
    """Resolve the mirror's live root external_id.

    When the operator pinned ``external_root_id`` we trust it. Without
    one, Shopware falls back to the sales-channel's
    ``navigationCategoryId``; Medusa has no equivalent and returns None
    (the stub adapter would raise on fetch_tree anyway).
    """
    if recorded:
        return recorded
    if mirror.backend == BACKEND_SHOPWARE and mirror.target_sales_channel:
        try:
            from ecommerce_integrations.catalog_mirror.engine.adapters.shopware import (
                resolve_sales_channel_root,
            )

            return resolve_sales_channel_root(mirror.target_sales_channel)
        except Exception:
            return None
    return None


def _load_mapping(mirror, erp_tree: ErpTreeNode) -> dict[str, str]:
    """Return the IG-name -> external_id map for the mirror's backend.

    Composed from two sources:

    1. The custom field ``<backend>_category_id`` on each IG in the
       walked subtree (persistent, survives renames).
    2. ``mirror.node_overrides`` rows with ``mode='pin'`` (manual
       overrides). Skip rows mean the IG is dropped from the mapping
       and from the walk — handled in :func:`_apply_overrides` below.

    Overrides win when both sources disagree.
    """
    field = _mapping_field(mirror.backend)
    if not field:
        return {}

    ig_names = [n.name for n in erp_tree.iter_descendants()]
    if not ig_names:
        return {}

    placeholders = ", ".join(["%s"] * len(ig_names))
    rows = frappe.db.sql(
        f"""SELECT name, {field} AS external_id
            FROM `tabItem Group`
            WHERE name IN ({placeholders})""",
        tuple(ig_names),
        as_dict=True,
    )
    mapping = {r.name: r.external_id for r in rows if r.external_id}

    for override in mirror.node_overrides or []:
        if (override.mode or "pin") == "skip":
            mapping.pop(override.item_group, None)
            continue
        if override.external_id:
            mapping[override.item_group] = override.external_id
    return mapping


def _persist_mapping(backend: str, item_group: str, external_id: str) -> None:
    """Write ``<backend>_category_id`` back onto the Item Group.

    Called inline as each create/update completes so a partial run
    still leaves the DB consistent with the backend state.
    """
    field = _mapping_field(backend)
    if not field or not item_group:
        return
    frappe.db.set_value(
        "Item Group",
        item_group,
        {field: external_id or ""},
        update_modified=False,
    )


def _mapping_field(backend: str) -> str | None:
    if backend == BACKEND_SHOPWARE:
        return "shopware_category_id"
    if backend == BACKEND_MEDUSA:
        return "medusa_category_id"
    return None


def _find_root_create(creates: Iterable[NodePlan], root_name: str) -> NodePlan | None:
    for plan in creates:
        if plan.item_group == root_name:
            return plan
    return None


def _move_depth(plan: NodePlan) -> int:
    # The differ doesn't carry explicit depth; the path field's
    # separator count is a cheap proxy.
    return (plan.item_group_path or "").count(" / ")


def _record_failure(mirror, log_name, exc, short_msg: str) -> MirrorRunResult:
    now = now_datetime()
    frappe.db.set_value(
        _MIRROR_DOCTYPE, mirror.name,
        {
            "sync_status": "error",
            "last_error": short_msg[:500],
            "last_synced_at": now,
            "last_heartbeat_at": now,
        },
        update_modified=False,
    )
    frappe.db.commit()
    _finish_log(log_name, status="Error", error=str(exc))
    return MirrorRunResult(
        mirror=mirror.name,
        backend=mirror.backend or "",
        status="error",
        errors=[short_msg],
    )


def _start_log(mirror) -> str | None:
    if not frappe.db.exists("DocType", _LOG_DOCTYPE):
        return None
    # ``integration`` is a Link to Module Def — must use the Module Def name
    # (``shopware6`` / ``medusa``), not the user-facing backend label
    # (``Shopware`` / ``Medusa``) or its lowercase form (``shopware``).
    # See product_sync/constants.py — same bug pattern as the Product Sync
    # orchestrator used to have.
    from ecommerce_integrations.product_sync.constants import (
        BACKEND_TO_INTEGRATION_KEY,
    )
    log = frappe.new_doc(_LOG_DOCTYPE)
    log.integration = (
        BACKEND_TO_INTEGRATION_KEY.get(mirror.backend or "")
        or "catalog_mirror"
    )
    log.method = (
        f"catalog_mirror.tasks.apply_mirror({mirror.name})"
    )
    log.title = f"Mirror {mirror.title} -> {mirror.backend}"
    log.status = "Queued"
    log.flags.ignore_permissions = True
    log.insert()
    return log.name


def _finish_log(
    log_name: str | None, *, status: str, message=None, error: str | None = None,
) -> None:
    if not log_name:
        return
    update: dict = {"status": status}
    if message is not None:
        import json

        update["response_data"] = json.dumps(message, default=str)
    if error is not None:
        update["traceback"] = error
    frappe.db.set_value(_LOG_DOCTYPE, log_name, update, update_modified=False)


def _is_backend_disabled(backend: str) -> bool:
    """Return True when the backend's master enable_* toggle is off.

    Mirrors the same guard used by smart_collections.tasks. The session
    decorators return None silently when disabled — without this check
    the apply loop would crash mid-execution between the status-flip to
    'running' and the terminal status-flip, leaving the mirror stranded
    until recover_stale_mirrors sweeps it 30 minutes later.
    """
    try:
        if backend == "Shopware":
            return not bool(frappe.db.get_single_value(
                "Shopware Setting", "enable_shopware",
            ))
        if backend == "Medusa":
            return not bool(frappe.db.get_single_value(
                "Medusa Setting", "enable_medusa",
            ))
    except Exception:
        # Fresh install before settings doctype exists — treat as
        # disabled so we never attempt a sync against unconfigured
        # state.
        return True
    return False
