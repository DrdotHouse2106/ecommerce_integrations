"""Whitelisted endpoints for the Catalog Mirror Dashboard page.

The dashboard (``catalog-mirror-dashboard``) consolidates the
per-mirror Preview / Adopt / Apply flow into one operator-facing page.
These endpoints sit alongside ``catalog_mirror/api.py`` (which powers
the single-mirror form). They are intentionally separate so the
dashboard's bulk semantics don't bleed into the single-mirror flow.

Permission model:

- ``list_with_suggestions`` requires *read* on the Mirror doctype.
- ``preview_all`` requires *read* on the Mirror doctype (preview is
  dry-run only).
- ``sync_all_adopted`` requires *write* on each mirror it enqueues;
  mirrors the operator can't write are silently skipped.
- ``adopt_root`` requires *write* on the target mirror.
"""

from __future__ import annotations

import frappe
from frappe import _

from ecommerce_integrations.catalog_mirror.constants import (
    BACKEND_MEDUSA,
    BACKEND_SHOPWARE,
    KNOWN_BACKENDS,
)

_MIRROR_DOCTYPE = "Ecommerce Catalog Mirror"
_KNOWN_ADOPT_MODES = ("link_only", "full_sync")


# ── Listing ──────────────────────────────────────────────────────────


@frappe.whitelist()
def list_with_suggestions(backend: str | None = None) -> list[dict]:
    """Return every Catalog Mirror with state + adopt-suggestions.

    For mirrors without ``external_root_id`` we ask the registered
    adapter for matching backend categories (via
    :func:`_safe_suggestions`). Adapter errors are swallowed and
    surfaced as an empty ``suggestions`` list — the dashboard must
    keep rendering even when one backend is misconfigured.

    Shape per row::

        {
          name, title, backend, root_item_group,
          target_sales_channel, target_sales_channel_data,
          external_root_id, sync_status, last_synced_at,
          last_error, is_active,
          suggestions: [
            {external_id, name, path, linked_product_count},
            ...
          ],
        }
    """
    if not frappe.has_permission(_MIRROR_DOCTYPE, "read"):
        frappe.throw(_("Not permitted to read Catalog Mirrors"))

    filters: dict = {}
    if backend:
        if backend not in KNOWN_BACKENDS:
            frappe.throw(_("Unknown backend: {0}").format(backend))
        filters["backend"] = backend

    docs = frappe.get_all(
        _MIRROR_DOCTYPE,
        filters=filters,
        fields=[
            "name",
            "title",
            "backend",
            "is_active",
            "root_item_group",
            "target_sales_channel",
            "target_sales_channel_data",
            "external_root_id",
            "sync_status",
            "last_synced_at",
            "last_error",
        ],
        order_by="title asc",
    )

    out: list[dict] = []
    for row in docs:
        row["suggestions"] = []
        if not row.get("external_root_id") and row.get("root_item_group"):
            row["suggestions"] = _safe_suggestions(
                row["name"], row["root_item_group"],
            )
        out.append(row)
    return out


def _safe_suggestions(mirror: str, item_group: str) -> list[dict]:
    """Wrap ``find_matching_nodes`` so a single misconfigured adapter
    doesn't blank the whole dashboard. Failures degrade to an empty
    suggestion list — the per-mirror form still shows the full error.
    """
    try:
        from ecommerce_integrations.catalog_mirror.api import (
            find_matching_nodes,
        )

        return find_matching_nodes(mirror=mirror, item_group=item_group) or []
    except Exception:
        # Swallow — the dashboard is best-effort; the operator can
        # still drill into the mirror form for a real error.
        return []


# ── Bulk preview ─────────────────────────────────────────────────────


@frappe.whitelist()
def preview_all(backend: str | None = None) -> dict:
    """Run preview_mirror on every active mirror; return aggregate.

    Per-mirror failures are captured in ``per_mirror[].error`` so the
    operator can see exactly which mirror needs attention without one
    bad backend tanking the whole call.
    """
    if not frappe.has_permission(_MIRROR_DOCTYPE, "read"):
        frappe.throw(_("Not permitted to read Catalog Mirrors"))

    filters: dict = {"is_active": 1}
    if backend:
        if backend not in KNOWN_BACKENDS:
            frappe.throw(_("Unknown backend: {0}").format(backend))
        filters["backend"] = backend

    names = frappe.get_all(
        _MIRROR_DOCTYPE, filters=filters, pluck="name", order_by="title asc",
    )

    from ecommerce_integrations.catalog_mirror.tasks import apply_mirror

    per_mirror: list[dict] = []
    totals = {"create": 0, "update": 0, "move": 0, "noop": 0, "orphan": 0, "error": 0}

    for name in names:
        title = frappe.db.get_value(_MIRROR_DOCTYPE, name, "title") or name
        backend_label = frappe.db.get_value(_MIRROR_DOCTYPE, name, "backend") or ""
        entry: dict = {"mirror": name, "title": title, "backend": backend_label}
        try:
            plan = apply_mirror(name, dry_run=True)
            data = plan.to_dict() if hasattr(plan, "to_dict") else plan
            counts = {
                "create": len(data.get("creates") or []),
                "update": len(data.get("updates") or []),
                "move": len(data.get("moves") or []),
                "noop": len(data.get("noops") or []),
                "orphan": len(data.get("orphans") or []),
            }
            entry.update(counts)
            for k, v in counts.items():
                totals[k] += v
        except Exception as e:
            entry["error"] = str(e)[:240]
            totals["error"] += 1
        per_mirror.append(entry)

    return {"per_mirror": per_mirror, "totals": totals}


# ── Bulk sync ────────────────────────────────────────────────────────


@frappe.whitelist()
def sync_all_adopted(backend: str | None = None) -> dict:
    """Enqueue ``apply_mirror`` for every adopted (linked) mirror.

    A mirror is *adopted* when its ``external_root_id`` is set — that
    is the explicit signal the operator has wired the mirror to an
    existing backend category. Un-adopted mirrors are skipped so this
    bulk action stays *non-destructive*: it never lets an accidentally
    misconfigured mirror create a fresh top-level category tree.

    Inactive mirrors are also skipped. Per-mirror write permission is
    checked individually so a role-restricted operator can still drive
    the bulk path for the mirrors they own.

    Each apply runs via the background queue (queue=``long``). The
    dashboard polls ``list_with_suggestions`` to surface results.
    """
    if not frappe.has_permission(_MIRROR_DOCTYPE, "read"):
        frappe.throw(_("Not permitted to read Catalog Mirrors"))

    filters: dict = {}
    if backend:
        if backend not in KNOWN_BACKENDS:
            frappe.throw(_("Unknown backend: {0}").format(backend))
        filters["backend"] = backend

    rows = frappe.get_all(
        _MIRROR_DOCTYPE,
        filters=filters,
        fields=["name", "is_active", "external_root_id"],
    )

    enqueued = 0
    skipped_unadopted = 0
    skipped_inactive = 0
    skipped_permission = 0

    for row in rows:
        if not int(row.get("is_active") or 0):
            skipped_inactive += 1
            continue
        if not row.get("external_root_id"):
            skipped_unadopted += 1
            continue
        if not frappe.has_permission(_MIRROR_DOCTYPE, "write", doc=row["name"]):
            skipped_permission += 1
            continue

        frappe.enqueue(
            "ecommerce_integrations.catalog_mirror.tasks.apply_mirror",
            queue="long",
            job_name=f"catalog_mirror.apply.{row['name']}",
            mirror_name=row["name"],
            dry_run=False,
            deduplicate=True,
        )
        enqueued += 1

    return {
        "enqueued": enqueued,
        "skipped_unadopted": skipped_unadopted,
        "skipped_inactive": skipped_inactive,
        "skipped_permission": skipped_permission,
    }


# ── Adopt root ───────────────────────────────────────────────────────


@frappe.whitelist()
def adopt_root(mirror: str, external_id: str, mode: str = "link_only") -> dict:
    """Wire ``external_id`` onto the mirror's ``external_root_id``.

    ``mode`` is preserved on the wire for forward compatibility:

    - ``link_only`` — set ``external_root_id`` only. (If a future
      schema adds a ``link_only`` flag to ``Ecommerce Catalog Mirror``
      or to ``Ecommerce Catalog Mirror Node Override``, this branch
      will additionally set it; see the inline check below.)
    - ``full_sync`` — set ``external_root_id`` only. The next live
      apply will create/update/move every child category.

    Today both modes have identical persisted effect because no
    ``link_only`` flag exists yet. Keeping the mode parameter means
    the dashboard wiring doesn't change when the flag lands.
    """
    if not frappe.has_permission(_MIRROR_DOCTYPE, "write", doc=mirror):
        frappe.throw(_("Not permitted to update this Catalog Mirror"))
    if mode not in _KNOWN_ADOPT_MODES:
        frappe.throw(_("mode must be one of: {0}").format(", ".join(_KNOWN_ADOPT_MODES)))
    if not external_id:
        frappe.throw(_("external_id is required"))

    doc = frappe.get_doc(_MIRROR_DOCTYPE, mirror)
    doc.external_root_id = external_id

    # Forward-compatible: if a link_only flag is added later (either to
    # the Mirror itself or to every Node Override row) honour the
    # mode here without a follow-up patch.
    if mode == "link_only":
        if hasattr(doc, "link_only"):
            doc.link_only = 1
        for row in (doc.node_overrides or []):
            if hasattr(row, "link_only"):
                row.link_only = 1

    doc.save(ignore_permissions=False)

    return {
        "mirror": mirror,
        "external_root_id": external_id,
        "mode": mode,
    }


# Re-export backend constants so a future caller can import them from
# the dashboard module without reaching into ``constants`` directly.
__all__ = [
    "BACKEND_MEDUSA",
    "BACKEND_SHOPWARE",
    "adopt_root",
    "list_with_suggestions",
    "preview_all",
    "sync_all_adopted",
]
