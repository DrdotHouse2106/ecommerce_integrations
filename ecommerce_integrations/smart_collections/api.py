"""Whitelisted API endpoints for the Smart Collections UI overlays.

Used by the JS form scripts on Medusa Setting and Shopware Setting to render a
read-only summary of Smart Collections that target the open Setting's backend.
The Settings forms link to the actual Smart Collection list for management;
this endpoint is purely a derived view.
"""

import frappe

from ecommerce_integrations.smart_collections.constants import KNOWN_BACKENDS


@frappe.whitelist()
def list_for_backend(backend: str) -> list[dict]:
    """Return all collection × target rows for one backend.

    The shape is flat (one row per target) so the JS can render it as a
    table grouped by ``sales_channel`` without a second round-trip.
    """
    if backend not in KNOWN_BACKENDS:
        frappe.throw(f"Unknown backend: {backend!r}")

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
        frappe.throw("Not permitted to preview this collection")
    return dry_run(frappe.get_doc("Ecommerce Smart Collection", collection))


@frappe.whitelist()
def toggle_target(target_id: str, enabled: int) -> None:
    """Toggle ``enabled`` on a single target row from the Setting widget.

    Permission: any user who can write the parent Smart Collection (the
    write check is enforced by the parent reload/save below).
    """
    target = frappe.get_doc("Ecommerce Smart Collection Target", target_id)
    if target.parenttype != "Ecommerce Smart Collection":
        frappe.throw("Not a Smart Collection target")
    parent = frappe.get_doc(target.parenttype, target.parent)
    parent.check_permission("write")
    target.db_set("enabled", 1 if int(enabled) else 0, update_modified=True)
