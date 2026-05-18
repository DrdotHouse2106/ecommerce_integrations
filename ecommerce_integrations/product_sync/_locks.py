"""Sync-claim / release primitives shared by push and pull orchestrators.

Both ``product_sync.tasks`` (push) and ``product_sync.pull_tasks`` (pull)
need the same three operations on their respective master doctypes:

- ``claim_sync`` — atomically set ``sync_status='running'`` so two
  schedulers can't double-fire the same Sync.
- ``release_sync`` — set the post-run status + ``last_synced_at`` +
  truncated ``last_error``.
- ``heartbeat_run`` — touch a Run row's ``modified`` so a stale-recover
  sweep can detect a dead worker.

The implementations only differ in *which doctype* they target, so
parameterising on doctype name keeps both call sites honest. Each call
commits its own transaction because schedulers run inside their own
worker process — leaving the claim row uncommitted would let a second
worker claim the same sync.
"""

from __future__ import annotations

import frappe
from frappe.utils import now_datetime

from ecommerce_integrations.product_sync.constants import STATUS_RUNNING


def claim_sync(doctype: str, sync_name: str) -> bool:
    """Atomically set ``sync_status='running'`` on ``doctype``/``sync_name``.

    Returns True if this caller acquired the lock, False if another
    worker had already claimed it. The UPDATE uses a guard predicate so
    two concurrent workers can't both flip the row to ``running``; a
    confirm-read after the UPDATE works around Frappe's
    ``frappe.db.sql`` not exposing the affected-row count.
    """
    frappe.db.sql(
        f"""UPDATE `tab{doctype}`
           SET sync_status = %s, last_heartbeat_at = %s, last_error = ''
           WHERE name = %s AND (sync_status IS NULL OR sync_status != %s)""",
        (STATUS_RUNNING, now_datetime(), sync_name, STATUS_RUNNING),
    )
    current = frappe.db.get_value(doctype, sync_name, "sync_status")
    if current == STATUS_RUNNING:
        frappe.db.commit()  # noqa: SLF001
        return True
    return False


def release_sync(
    doctype: str,
    sync_name: str,
    *,
    status: str,
    last_error: str | None,
) -> None:
    """Set post-run status + timestamps on ``doctype``/``sync_name``.

    ``last_error`` is truncated to 500 chars so a verbose traceback
    can't push the row past column limits.
    """
    frappe.db.set_value(
        doctype, sync_name,
        {
            "sync_status": status,
            "last_synced_at": now_datetime(),
            "last_error": (last_error or "")[:500],
        },
        update_modified=False,
    )
    frappe.db.commit()  # noqa: SLF001


def heartbeat_run(run_doctype: str, run_name: str) -> None:
    """Touch the Run row's ``modified`` so stale-recover can find it."""
    frappe.db.set_value(
        run_doctype, run_name,
        {"finished_at": None},
        update_modified=True,
    )
