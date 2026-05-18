"""Shared helpers used by both Shopware and Medusa pull handlers.

The two backends paginate differently (Shopware uses ``POST /api/search/*``
with a ``Criteria`` object, Medusa uses ``GET /admin/*`` with
``offset/limit``) but they agree on:

- pagination caps (``_MAX_PER_RUN``, ``_PAGE_SIZE``)
- the watermark contract (fall back to ``now - lookback_hours`` when
  no successful pull has happened yet)
- per-page bulk-existence-check semantics (look up everything from
  the page in one SELECT, treat hits as "already synced, skip")

Keeping these in one place ensures the two pull handlers don't drift.
"""

from __future__ import annotations

from datetime import datetime

import frappe
from frappe.utils import add_to_date, get_datetime, now_datetime

# Hard cap per run. Keeps a runaway pull from hammering the API and
# blocking the queue; if more rows than this need catching up, the
# operator should drop ``lookback_hours`` and re-run.
MAX_PER_RUN = 1000
PAGE_SIZE = 100


def resolve_watermark(sync_doc, field: str) -> datetime:
    """Return the per-target watermark; fall back to lookback window."""
    raw = getattr(sync_doc, field, None)
    if raw:
        try:
            return get_datetime(raw)
        except Exception:
            pass
    hours = int(getattr(sync_doc, "lookback_hours", None) or 168)
    return add_to_date(now_datetime(), hours=-hours)


def existing_record_set(
    doctype: str, fieldname: str, candidate_ids: list[str],
) -> set[str]:
    """Bulk existence-check used by every pull handler.

    Replaces a per-record ``frappe.db.get_value(doctype, {fieldname: id})``
    with one indexed SELECT that returns every id from ``candidate_ids``
    already present in ``doctype.fieldname``. Caller treats the
    returned set as "skip — already in ERPNext".
    """
    if not candidate_ids:
        return set()
    rows = frappe.get_all(
        doctype,
        filters={fieldname: ("in", list(candidate_ids))},
        pluck=fieldname,
        limit=0,
    )
    return set(rows)
