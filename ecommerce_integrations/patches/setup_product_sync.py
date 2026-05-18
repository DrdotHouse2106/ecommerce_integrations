"""Install custom fields the Product Sync feature depends on.

Adds three fields to ``Ecommerce Item``:

- ``last_synced_hash`` — SHA-256 of the canonical payload last pushed
  to the backend. The Phase-3 differ compares the current payload's
  hash to this column and skips items whose hash didn't change. This
  is the foundation of reliable delta-detection — without it the
  scheduled sync would touch every item on every run.
- ``last_synced_at`` — when the last successful push happened. Used
  by ``apply_sync`` for "since-watermark" optimisations and by the
  Reconciliation-Run-mode (Phase 6) to find rows the regular cron
  has not visited in a while.
- ``last_sync_run`` — Link to the ``Ecommerce Sync Run`` that produced
  the current state. Lets the operator click through from any Item to
  the run that last touched it (for audit / debugging / rollback).

The doctypes ``Ecommerce Product Sync`` / ``... Channel`` /
``... Filter Rule`` / ``... Override`` / ``Ecommerce Sync Run`` /
``Ecommerce Sync Error`` are picked up automatically from the
``Product Sync`` module entry in ``modules.txt`` — no ``reload_doc``
needed here. This patch only owns the custom-field additions and a
post-conditions check.

Idempotent + no-op safe: ``create_custom_fields`` upserts by
``fieldname``, and the patch is a no-op when ``Ecommerce Item``
isn't on the site (e.g. a slimmer subset of this app).
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


_ECOMMERCE_ITEM_DOCTYPE = "Ecommerce Item"

_PRODUCT_SYNC_DOCTYPES = (
    "Ecommerce Product Sync",
    "Ecommerce Product Sync Channel",
    "Ecommerce Product Sync Filter Rule",
    "Ecommerce Product Sync Override",
    "Ecommerce Sync Run",
    "Ecommerce Sync Error",
)


PRODUCT_SYNC_CUSTOM_FIELDS = {
    _ECOMMERCE_ITEM_DOCTYPE: [
        {
            "fieldname": "sync_state_section",
            "label": "Sync State",
            "fieldtype": "Section Break",
            "insert_after": "integration_item_code",
            "collapsible": 1,
        },
        {
            "fieldname": "last_synced_hash",
            "label": "Last Synced Hash",
            "fieldtype": "Data",
            "length": 64,
            "read_only": 1,
            "no_copy": 1,
            "insert_after": "sync_state_section",
            "description": (
                "SHA-256 of the canonical payload last successfully pushed "
                "to the backend. Used for delta-detection: the differ "
                "skips items whose current hash equals this value. "
                "Written by Product Sync; do not edit manually."
            ),
        },
        {
            "fieldname": "last_synced_at",
            "label": "Last Synced At",
            "fieldtype": "Datetime",
            "read_only": 1,
            "no_copy": 1,
            "insert_after": "last_synced_hash",
            "description": (
                "Timestamp of the most recent successful push. Used by "
                "the Reconciliation-Run mode to find rows the regular "
                "cron hasn't visited recently."
            ),
        },
        {
            "fieldname": "last_sync_run",
            "label": "Last Sync Run",
            "fieldtype": "Link",
            "options": "Ecommerce Sync Run",
            "read_only": 1,
            "no_copy": 1,
            "insert_after": "last_synced_at",
            "description": (
                "Ecommerce Sync Run that last touched this row. Click "
                "through for the full audit trail."
            ),
        },
    ],
}


def execute() -> None:
    if not frappe.db.exists("DocType", _ECOMMERCE_ITEM_DOCTYPE):
        # Vanilla Frappe site without this app's core doctype installed.
        # The custom fields make no sense in isolation; skip cleanly.
        return

    create_custom_fields(PRODUCT_SYNC_CUSTOM_FIELDS, ignore_validate=True)

    # Post-conditions check: bench migrate will have picked up the six
    # doctypes from modules.txt before this patch runs. If any of them
    # is missing, surface a clear log entry so the operator can fix
    # their working tree (e.g. they pulled the patch but not the
    # doctype JSONs).
    missing = [
        dt for dt in _PRODUCT_SYNC_DOCTYPES if not frappe.db.exists("DocType", dt)
    ]
    if missing:
        frappe.log_error(
            title="Product Sync — missing doctypes after migrate",
            message=(
                "The setup_product_sync patch ran but the following "
                "doctypes are still missing:\n  - "
                + "\n  - ".join(missing)
                + "\n\nThis means modules.txt does not include 'Product Sync' "
                "or the doctype JSONs are not on disk. Pull the latest tree "
                "and re-run bench migrate."
            ),
        )
