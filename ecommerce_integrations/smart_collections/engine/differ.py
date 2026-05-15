"""Snapshot-based diff for Smart Collection sync planning.

Each ``(collection, target_idx)`` pair gets one ``Ecommerce Smart Collection
Snapshot`` document holding the last item-set the resolver produced. On the
next sync, the adapter compares the current set against the snapshot and
issues a minimal pair of ``link_items`` / ``unlink_items`` calls.

Snapshot names are deterministic (``{collection}-{target_idx}``) so a
repeated save updates in place rather than accumulating versions.
"""

import frappe
from frappe.utils import now_datetime

_SNAPSHOT_DOCTYPE = "Ecommerce Smart Collection Snapshot"


def _snapshot_name(collection_name: str, target_idx: int) -> str:
    return f"{collection_name}-{target_idx}"


def load_snapshot(collection_name: str, target_idx: int) -> set[str]:
    name = _snapshot_name(collection_name, target_idx)
    if not frappe.db.exists(_SNAPSHOT_DOCTYPE, name):
        return set()
    return frappe.get_doc(_SNAPSHOT_DOCTYPE, name).get_items()


def save_snapshot(collection_name: str, target_idx: int, item_codes) -> None:
    name = _snapshot_name(collection_name, target_idx)
    items = set(item_codes or ())
    if frappe.db.exists(_SNAPSHOT_DOCTYPE, name):
        snap = frappe.get_doc(_SNAPSHOT_DOCTYPE, name)
    else:
        snap = frappe.new_doc(_SNAPSHOT_DOCTYPE)
        snap.collection = collection_name
        snap.target_idx = target_idx
    snap.set_items(items)
    snap.snapshotted_at = now_datetime()
    snap.flags.ignore_permissions = True
    snap.save()


def compute_diff(collection_name: str, target_idx: int, new_items) -> dict:
    """Return ``{to_add, to_remove, to_keep}`` against the persisted snapshot.

    First-run case (no snapshot) returns ``new_items`` as ``to_add`` and
    empty ``to_remove`` / ``to_keep``. Callers persist the new snapshot
    only after the adapter has applied the diff successfully.
    """
    new_set = set(new_items or ())
    old_set = load_snapshot(collection_name, target_idx)
    return {
        "to_add": new_set - old_set,
        "to_remove": old_set - new_set,
        "to_keep": new_set & old_set,
    }
