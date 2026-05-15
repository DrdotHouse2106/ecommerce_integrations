"""Whitelisted API endpoints for the Smart Collections UI overlays.

Used by the JS form scripts on Medusa Setting and Shopware Setting to render a
read-only summary of Smart Collections that target the open Setting's backend.
The Settings forms link to the actual Smart Collection list for management;
this endpoint is purely a derived view.
"""

import frappe
from frappe import _

from ecommerce_integrations.smart_collections.constants import KNOWN_BACKENDS


@frappe.whitelist()
def list_for_backend(backend: str) -> list[dict]:
    """Return all collection × target rows for one backend.

    The shape is flat (one row per target) so the JS can render it as a
    table grouped by ``sales_channel`` without a second round-trip.
    """
    # SECURITY: read permission on the collection doctype gates the whole
    # view — without this any authenticated user can enumerate
    # smart-collection / sales-channel mappings.
    if not frappe.has_permission("Ecommerce Smart Collection", "read"):
        frappe.throw(_("Not permitted to read Smart Collections"))
    if backend not in KNOWN_BACKENDS:
        frappe.throw(_("Unknown backend: {0}").format(repr(backend)))

    rows = frappe.db.sql(
        """
        SELECT
            sc.name              AS collection,
            sc.title             AS title,
            sc.is_active         AS is_active,
            sc.last_resolved_count,
            sc.last_resolved_at,
            t.name               AS target_id,
            t.idx                AS target_idx,
            t.sales_channel,
            t.enabled,
            t.visibility,
            t.external_id,
            t.sync_status,
            t.last_synced_at,
            t.last_error
        FROM `tabEcommerce Smart Collection Target` t
        INNER JOIN `tabEcommerce Smart Collection` sc
            ON sc.name = t.parent
        WHERE t.parenttype = 'Ecommerce Smart Collection'
          AND t.backend = %s
        ORDER BY t.sales_channel ASC, sc.title ASC
        """,
        (backend,),
        as_dict=True,
    )
    return rows


@frappe.whitelist()
def preview(collection: str) -> dict:
    """Return the resolver's dry-run output for a saved collection.

    Used by the form's *Preview Matches* button — does not persist any
    state on the collection.
    """
    from ecommerce_integrations.smart_collections.engine.resolver import dry_run

    if not frappe.has_permission("Ecommerce Smart Collection", "read", doc=collection):
        frappe.throw(_("Not permitted to preview this collection"))
    return dry_run(frappe.get_doc("Ecommerce Smart Collection", collection))


@frappe.whitelist()
def preview_collection(collection: str) -> dict:
    """Return a dry-run preview as a JSON-serialisable dict.

    Used by the Smart Collection form's *Preview Sync* dialog to show:

    - per-target action (CREATE/UPDATE/NOOP) and the live diff against
      the backend's current state,
    - item-level add/remove/keep counts,
    - match suggestions for the Adopt flow when ``external_id`` is empty,
    - unresolved items (not yet exported to the backend).

    Requires *write* permission on the collection — preview-only access
    leaks information about backend state that read-only users
    shouldn't be able to probe through this endpoint.
    """
    if not frappe.has_permission(
        "Ecommerce Smart Collection", "write", doc=collection,
    ):
        frappe.throw(_("Not permitted to preview this collection"))
    from ecommerce_integrations.smart_collections.tasks import sync_collection

    plan = sync_collection(collection, dry_run=True)
    return plan.to_dict() if hasattr(plan, "to_dict") else plan


@frappe.whitelist()
def adopt_match(target_name: str, external_id: str, link_only: int = 0) -> dict:
    """Adopt an existing backend category by wiring its id onto ``target``.

    Used by the *Preview Sync* dialog when the adapter found an
    existing same-named category in the backend — the user clicks
    ``Adopt`` (optionally ``+ Link Only``) and the target row is
    updated in place. The next sync then *updates* that category
    instead of creating a duplicate.

    The ``link_only=1`` flavour additionally turns on the
    adopt-and-protect mode: the first sync only ADDS items and
    doesn't unlink anything already in the backend. Useful when the
    backend category was hand-curated and the operator only wants
    ERPNext rules to extend it, not overwrite it.
    """
    if not external_id:
        frappe.throw(_("external_id is required"))

    target = frappe.get_doc("Ecommerce Smart Collection Target", target_name)
    if target.parenttype != "Ecommerce Smart Collection":
        frappe.throw(_("Not a Smart Collection target"))
    parent = frappe.get_doc(target.parenttype, target.parent)
    parent.check_permission("write")

    update = {
        "external_id": external_id,
        "sync_status": "pending",
        "last_error": "",
    }
    if int(link_only or 0):
        update["link_only"] = 1
    target.db_set(update, update_modified=True)
    return {
        "target_name": target.name,
        "external_id": external_id,
        "link_only": int(link_only or 0),
    }


@frappe.whitelist()
def toggle_target(target_id: str, enabled: int) -> None:
    """Toggle ``enabled`` on a single target row from the Setting widget.

    Permission: any user who can write the parent Smart Collection (the
    write check is enforced by the parent reload/save below).
    """
    target = frappe.get_doc("Ecommerce Smart Collection Target", target_id)
    if target.parenttype != "Ecommerce Smart Collection":
        frappe.throw(_("Not a Smart Collection target"))
    parent = frappe.get_doc(target.parenttype, target.parent)
    parent.check_permission("write")
    target.db_set("enabled", 1 if int(enabled) else 0, update_modified=True)
