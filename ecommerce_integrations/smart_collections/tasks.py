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
"""

import frappe
from frappe.utils import now_datetime

from ecommerce_integrations.smart_collections.channel_visibility import (
    invalidate_cache,
)
from ecommerce_integrations.smart_collections.engine.adapters.base import (
    AdapterError,
    get_adapter,
)
from ecommerce_integrations.smart_collections.engine.differ import (
    compute_diff,
    save_snapshot,
)
from ecommerce_integrations.smart_collections.engine.resolver import resolve


_LOG_DOCTYPE = "Ecommerce Integration Log"
_COLLECTION_DOCTYPE = "Ecommerce Smart Collection"


@frappe.whitelist()
def sync_collection_now(collection: str) -> dict:
    """Whitelisted entry — used by the form button on a Smart Collection."""
    if not frappe.has_permission(_COLLECTION_DOCTYPE, "write", doc=collection):
        frappe.throw("Not permitted to sync this collection")
    return sync_collection(collection)


def sync_collection(collection_name: str) -> dict:
    """Sync one collection across all its enabled targets. Returns a
    summary dict with per-target outcomes."""
    coll = frappe.get_doc(_COLLECTION_DOCTYPE, collection_name)
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


def _sync_target(coll, target, items: set[str]) -> dict:
    log_name = _start_log(coll, target)
    try:
        adapter = get_adapter(target.backend)

        external_id = adapter.upsert_category(coll, target)
        if external_id and target.external_id != external_id:
            target.external_id = external_id

        diff = compute_diff(coll.name, target.idx, items)

        unresolved = adapter.link_items(target, sorted(diff["to_add"])) or []
        adapter.unlink_items(target, sorted(diff["to_remove"]))

        # Snapshot only items the adapter actually managed — items that
        # haven't been pushed to the backend yet should not be considered
        # "linked" for the next diff.
        unresolved_set = set(unresolved)
        synced_items = {i for i in items if i not in unresolved_set}
        save_snapshot(coll.name, target.idx, synced_items)

        now = now_datetime()
        frappe.db.set_value(
            "Ecommerce Smart Collection Target", target.name,
            {
                "external_id": target.external_id or "",
                "sync_status": "ok",
                "last_synced_at": now,
                "last_error": "",
            },
            update_modified=False,
        )

        _finish_log(
            log_name,
            status="Success",
            message={
                "to_add": len(diff["to_add"]),
                "to_remove": len(diff["to_remove"]),
                "to_keep": len(diff["to_keep"]),
                "unresolved": len(unresolved),
            },
        )
        return {
            "target_idx": target.idx,
            "backend": target.backend,
            "sales_channel": target.sales_channel,
            "status": "ok",
            "added": len(diff["to_add"]) - len(unresolved),
            "removed": len(diff["to_remove"]),
            "unresolved": len(unresolved),
        }

    except AdapterError as e:
        return _record_failure(coll, target, log_name, e, str(e))
    except Exception as e:
        return _record_failure(
            coll, target, log_name, e, f"unexpected error: {type(e).__name__}"
        )


def _record_failure(coll, target, log_name, exc, short_msg: str) -> dict:
    frappe.db.set_value(
        "Ecommerce Smart Collection Target", target.name,
        {
            "sync_status": "error",
            "last_error": short_msg[:500],
            "last_synced_at": now_datetime(),
        },
        update_modified=False,
    )
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
