"""Backfill the new ``linked_smart_collections`` child-table.

Older Product Sync docs stored exactly one smart collection in the
``linked_smart_collection`` Link field. The table-replacement migration
keeps the legacy field as hidden read-only (so foreign installs don't
lose data on first migrate) but moves the value into the new table so
the walker's child-table path is the single source of truth.

Idempotent: only adds a row when the table is empty for that Sync, and
the legacy value still references an existing Smart Collection.
"""

from __future__ import annotations

import frappe

_SYNC = "Ecommerce Product Sync"
_TABLE = "Ecommerce Product Sync Smart Collection"
_COLLECTION = "Ecommerce Smart Collection"


def execute() -> None:
    if not frappe.db.exists("DocType", _SYNC):
        return
    if not frappe.db.exists("DocType", _TABLE):
        # Child doctype hasn't been imported yet; nothing to do.
        return

    rows = frappe.db.sql(
        """
        SELECT name, linked_smart_collection
        FROM `tabEcommerce Product Sync`
        WHERE COALESCE(linked_smart_collection, '') != ''
        """,
        as_dict=True,
    )
    if not rows:
        return

    for row in rows:
        sync_name = row["name"]
        legacy = (row["linked_smart_collection"] or "").strip()
        if not legacy:
            continue
        if not frappe.db.exists(_COLLECTION, legacy):
            continue
        existing = frappe.db.count(
            _TABLE,
            filters={"parent": sync_name, "parenttype": _SYNC},
        )
        if existing:
            # Operator already populated the table; don't double-up.
            continue
        child = frappe.get_doc({
            "doctype": _TABLE,
            "smart_collection": legacy,
            "sales_channel_id": "",
            "parent": sync_name,
            "parenttype": _SYNC,
            "parentfield": "linked_smart_collections",
        })
        child.flags.ignore_permissions = True
        child.insert()
    frappe.db.commit()  # noqa: SLF001 — persist backfill before patches.txt advances
