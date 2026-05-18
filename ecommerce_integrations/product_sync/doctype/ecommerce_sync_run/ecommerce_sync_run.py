"""Ecommerce Sync Run — one row per ``apply_sync`` invocation.

Audit-trail / event-sourcing entity. The dataclass
:class:`product_sync.engine.preview.ProductSyncRunResult` is the
in-memory shape that the orchestrator builds during a run; this
doctype is its persisted, queryable mirror. Operators read the form
list to answer "what did the last sync do?", to spot regressions, and
to invoke the rollback path via ``snapshot_json``.

Read-only from a user perspective — the orchestrator writes the row
with ``ignore_permissions=True``; the form view is for inspection.
"""

from __future__ import annotations

from frappe.model.document import Document


class EcommerceSyncRun(Document):
    pass
