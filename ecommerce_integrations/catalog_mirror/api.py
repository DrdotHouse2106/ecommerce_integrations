"""Whitelisted API endpoints for the Catalog Mirror UI overlays.

Used by the *Preview Mirror* and *Apply Mirror* buttons on the
Ecommerce Catalog Mirror form, plus the Adopt / Orphan row controls in
the preview dialog (Phase 3) and the per-Item resolver indicator on
the Item form (Phase 4).

Permission model:

- ``preview_mirror`` requires *read* on the Mirror doc — the preview is
  derived from existing state, no writes.
- ``apply_mirror_now`` requires *write* — the live path mutates backend
  state and persists ``<backend>_category_id`` on Item Groups.
- ``adopt_node`` requires *write* (it appends a node_override row).
- ``set_orphan_action`` requires *write* (it can trigger a delete).
- ``resolve_item_for_form`` requires *read* on the Item — pure read
  through the resolver, no writes.
"""

from __future__ import annotations

import frappe
from frappe import _

from ecommerce_integrations.catalog_mirror.constants import (
    BACKEND_MEDUSA,
    BACKEND_SHOPWARE,
    ORPHAN_DELETE,
    ORPHAN_KEEP,
    ORPHAN_REPORT,
)


_MIRROR_DOCTYPE = "Ecommerce Catalog Mirror"
_OVERRIDE_DOCTYPE = "Ecommerce Catalog Mirror Node Override"
_KNOWN_ORPHAN_ACTIONS = (ORPHAN_KEEP, ORPHAN_DELETE, ORPHAN_REPORT, "adopt")


@frappe.whitelist()
def preview_mirror(mirror: str) -> dict:
    """Return the dry-run preview for ``mirror``.

    Read-only — does not mutate the mirror's sync_status or the backend.
    """
    if not frappe.has_permission(_MIRROR_DOCTYPE, "read", doc=mirror):
        frappe.throw(_("Not permitted to preview this Catalog Mirror"))
    from ecommerce_integrations.catalog_mirror.tasks import apply_mirror

    plan = apply_mirror(mirror, dry_run=True)
    return plan.to_dict() if hasattr(plan, "to_dict") else plan


@frappe.whitelist()
def apply_mirror_now(mirror: str) -> dict:
    """Apply the mirror's diff against the live backend.

    Write-only — guarded by ``write`` permission so a read-only user
    can't trigger backend mutations through this endpoint.
    """
    if not frappe.has_permission(_MIRROR_DOCTYPE, "write", doc=mirror):
        frappe.throw(_("Not permitted to apply this Catalog Mirror"))
    from ecommerce_integrations.catalog_mirror.tasks import apply_mirror

    result = apply_mirror(mirror, dry_run=False)
    return result.to_dict() if hasattr(result, "to_dict") else result


@frappe.whitelist()
def adopt_node(
    mirror: str,
    item_group: str,
    external_id: str,
    mode: str = "pin",
) -> dict:
    """Wire ``external_id`` onto ``item_group`` via a node_override row.

    The override row makes the manual mapping survive across deletes /
    re-walks; the IG's ``<backend>_category_id`` is set immediately so
    the next preview already reflects the adoption.
    """
    if not frappe.has_permission(_MIRROR_DOCTYPE, "write", doc=mirror):
        frappe.throw(_("Not permitted to update this Catalog Mirror"))
    if mode not in ("pin", "skip"):
        frappe.throw(_("mode must be 'pin' or 'skip'"))
    if mode == "pin" and not external_id:
        frappe.throw(_("external_id is required for mode='pin'"))

    parent = frappe.get_doc(_MIRROR_DOCTYPE, mirror)

    # Replace any existing override for the same item_group so the
    # adopt action is idempotent (clicking it twice doesn't stack rows).
    existing = [
        row for row in (parent.node_overrides or [])
        if row.item_group == item_group
    ]
    for row in existing:
        parent.remove(row)

    parent.append(
        "node_overrides",
        {
            "item_group": item_group,
            "external_id": external_id or "",
            "mode": mode,
        },
    )
    parent.save(ignore_permissions=False)

    # Persist the mapping inline so the next preview already reflects
    # the adoption.
    from ecommerce_integrations.catalog_mirror.tasks import (
        _mapping_field,
        _persist_mapping,
    )

    field = _mapping_field(parent.backend)
    if mode == "pin" and field:
        _persist_mapping(parent.backend, item_group, external_id)
    elif mode == "skip" and field:
        _persist_mapping(parent.backend, item_group, "")

    return {
        "mirror": mirror,
        "item_group": item_group,
        "external_id": external_id if mode == "pin" else None,
        "mode": mode,
    }


@frappe.whitelist()
def set_orphan_action(
    mirror: str, external_id: str, action: str,
) -> dict:
    """Per-orphan action triggered from the preview dialog.

    ``action`` is one of ``delete`` / ``keep`` / ``report`` / ``adopt``.
    ``delete`` invokes the adapter's ``delete_node`` immediately;
    ``keep`` and ``report`` are advisory (the next preview will surface
    the same orphan).

    ``adopt`` is *not* fulfilled here on purpose — adopting requires an
    ``item_group`` target, so the JS sends a follow-up ``adopt_node``
    call. This endpoint exists so the orphan row's quick-delete works
    without round-tripping through the Adopt dialog.
    """
    if not frappe.has_permission(_MIRROR_DOCTYPE, "write", doc=mirror):
        frappe.throw(_("Not permitted to update this Catalog Mirror"))
    if action not in _KNOWN_ORPHAN_ACTIONS:
        frappe.throw(
            _("Unknown orphan action: {0}").format(action),
        )
    if not external_id:
        frappe.throw(_("external_id is required"))

    parent = frappe.get_doc(_MIRROR_DOCTYPE, mirror)

    if action == ORPHAN_DELETE:
        from ecommerce_integrations.catalog_mirror.engine.adapters.base import (
            AdapterError,
            get_adapter,
        )

        adapter = get_adapter(parent.backend)
        try:
            adapter.delete_node(external_id)
        except (AdapterError, NotImplementedError) as e:
            frappe.throw(_("Backend refused delete: {0}").format(e))
        return {
            "mirror": mirror,
            "external_id": external_id,
            "action": action,
            "deleted": True,
        }

    # keep / report / adopt are advisory at this endpoint.
    return {
        "mirror": mirror,
        "external_id": external_id,
        "action": action,
        "deleted": False,
    }


@frappe.whitelist()
def find_matching_nodes(mirror: str, item_group: str) -> list[dict]:
    """Backend candidates an Item Group could be adopted to.

    Powers the Phase 3 Adopt sub-dialog: the operator picks an
    unmapped IG, this endpoint resolves the Mirror's adapter, computes
    the *proposed* backend name (via the Mirror's ``name_template``)
    and the *proposed* parent ``external_id`` (the parent IG's
    ``<backend>_category_id`` — or the Mirror's ``external_root_id``
    when the IG is the root), then asks the adapter to surface every
    backend category named like that under that parent.

    Read-only — only the Mirror's ``read`` permission is required; we
    don't mutate anything here. The actual write lands in
    :func:`adopt_node` after the operator picks one of the returned
    candidates.
    """
    if not frappe.has_permission(_MIRROR_DOCTYPE, "read", doc=mirror):
        frappe.throw(_("Not permitted to read this Catalog Mirror"))

    doc = frappe.get_doc(_MIRROR_DOCTYPE, mirror)
    ig = frappe.get_doc("Item Group", item_group)

    # Compute the proposed backend name via the Mirror's template so
    # the adapter searches for what the next sync would actually create
    # — not the bare IG name (which can differ when operators use a
    # prefix/suffix template).
    proposed_name = frappe.render_template(
        doc.name_template or "{{ item_group.item_group_name }}",
        {"item_group": ig.as_dict()},
    )

    # Compute the proposed parent external_id. If the IG has a parent
    # IG, use that parent's ``<backend>_category_id``; otherwise fall
    # back to the Mirror's pinned ``external_root_id``. ``None`` is a
    # valid input to the adapter — it falls back to a global search.
    if ig.parent_item_group:
        field = _backend_category_field(doc.backend)
        parent_external_id = None
        if field:
            parent_external_id = frappe.db.get_value(
                "Item Group", ig.parent_item_group, field,
            )
        if not parent_external_id:
            # Fall back to the Mirror root when the immediate parent
            # has no mapping yet — typical on first adoption after a
            # fresh install.
            parent_external_id = doc.external_root_id or None
    else:
        parent_external_id = doc.external_root_id or None

    from ecommerce_integrations.catalog_mirror.engine.adapters.base import (
        AdapterError,
        get_adapter,
    )

    try:
        adapter = get_adapter(doc.backend)
        matches = adapter.find_matching_nodes(
            proposed_name, parent_external_id,
        )
    except (AdapterError, NotImplementedError) as e:
        frappe.throw(_("Adapter refused match search: {0}").format(e))

    return [
        {
            "external_id": m.external_id,
            "name": m.name,
            "path": m.path,
            "linked_product_count": m.linked_product_count,
        }
        for m in matches
    ]


def _backend_category_field(backend: str) -> str | None:
    if backend == BACKEND_SHOPWARE:
        return "shopware_category_id"
    if backend == BACKEND_MEDUSA:
        return "medusa_category_id"
    return None


@frappe.whitelist()
def resolve_item_for_form(item_code: str) -> dict:
    """Return the unified resolution for the Item form indicator.

    Read-only — only requires *read* permission on the Item. Returns
    the resolution for BOTH backends so the form can render channel +
    category placement in a single round-trip.
    """
    if not frappe.has_permission("Item", "read", doc=item_code):
        frappe.throw(_("Not permitted to read this Item"))

    from ecommerce_integrations.catalog_mirror.resolver import resolve_item

    return {
        "item_code": item_code,
        BACKEND_SHOPWARE.lower(): resolve_item(
            item_code, BACKEND_SHOPWARE,
        ).to_dict(),
        BACKEND_MEDUSA.lower(): resolve_item(
            item_code, BACKEND_MEDUSA,
        ).to_dict(),
    }
