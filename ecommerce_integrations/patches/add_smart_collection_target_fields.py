"""Add ``link_only`` + ``last_heartbeat_at`` to Smart Collection Target.

Also widens ``sync_status`` Select options from ``pending/ok/error`` to
``pending/running/ok/error`` so the heartbeat-driven running-state
flow has a value to write.

The actual schema lives in the doctype JSON — this patch just reloads
the doctype so ``bench migrate`` picks up the field additions and the
new Select option in one place.

Idempotent: reload_doc is naturally idempotent, and the patch is a
no-op when Smart Collections isn't installed on this site.
"""

import frappe


def execute():
    if not frappe.db.exists("DocType", "Ecommerce Smart Collection Target"):
        return

    frappe.reload_doc(
        "smart_collections",
        "doctype",
        "ecommerce_smart_collection_target",
        force=True,
    )

    # Older rows may carry NULL sync_status — normalise them so the
    # form's red-dashboard indicator counts behave predictably.
    frappe.db.sql(
        """UPDATE `tabEcommerce Smart Collection Target`
           SET sync_status = 'pending'
           WHERE sync_status IS NULL OR sync_status = ''"""
    )
