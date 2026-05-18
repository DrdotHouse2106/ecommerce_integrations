"""Ecommerce Sync Error — one row per (sync, item, failure) triple.

Three-tier DLQ pattern (severity = ``immediate`` / ``transient`` /
``manual_review``) modelled after Netflix's payment-pipeline. The
retry dispatcher polls this doctype, picks rows whose ``next_retry_at``
is due, re-enqueues them, and on success flips ``resolved=1``.

Severity contract:

- ``immediate`` — validation errors (4xx with deterministic cause).
  No automatic retry; operator must read the payload and fix the data.
- ``transient`` — timeouts, 5xx, rate-limit hits. The dispatcher
  re-enqueues with exponential backoff (next_retry_at advances on
  each attempt).
- ``manual_review`` — business-logic conflicts (SKU already taken,
  inventory disagreement). Sits silently in the queue until the
  operator flips ``resolved`` after a manual decision.

``resolved`` is the active-vs-archived flag. Filtering the list view
by ``resolved=0`` shows the operator's current backlog.
"""

from __future__ import annotations

import frappe
from frappe.model.document import Document
from frappe.utils import now_datetime


class EcommerceSyncError(Document):
    def before_save(self) -> None:
        # Stamp resolution metadata automatically so the resolver path
        # doesn't have to do it from every call site.
        if self.resolved and not self.resolved_at:
            self.resolved_at = now_datetime()
            self.resolved_by = frappe.session.user
