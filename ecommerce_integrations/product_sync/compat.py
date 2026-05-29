"""Compatibility shims that route legacy entry points through the
new ``product_sync`` engine.

The legacy single-item upload contract (used by ``sync_manager`` and
``reconciliation``) returns the Shopware integration id on success
and ``None`` on failure. Callers branch on truthiness:

    result = upload_erpnext_item_to_shopware(item_code)
    if result:
        stats["synced"] += 1
    else:
        stats["error_count"] += 1

To preserve those call sites without touching every branch, this
module wraps :func:`product_sync.tasks.dispatch_item_change` and
adapts the result back to the legacy id-or-None shape. New code
should call ``dispatch_item_change`` directly — this module exists
only to bridge the admin-reconciliation flow until those callers
are themselves rewritten to use plain ``apply_sync``.
"""

from __future__ import annotations

import frappe

from ecommerce_integrations.product_sync.constants import BACKEND_SHOPWARE
from ecommerce_integrations.product_sync.tasks import dispatch_item_change


def push_item_via_engine(item_code: str, backend: str = BACKEND_SHOPWARE) -> str | None:
    """Run a single-item delta sync via the new engine, return the
    backend integration id on success (truthy) or ``None`` on failure
    (falsy). The contract matches the legacy
    ``upload_erpnext_item_to_shopware`` so admin reconciliation
    loops can swap the call without restructuring their stats /
    error-handling branches.

    A noop (item unchanged since last sync) counts as success —
    same as the legacy uploader, which also returned the existing
    id when the upload was a no-op PATCH.
    """
    result = dispatch_item_change(item_code, backend) or {}
    run = result.get(backend)
    if run is None:
        return None
    # The new engine's status values are ``ok`` (all items succeeded)
    # and ``partial`` (some items succeeded). For a single-item
    # subset both indicate a successful push or a clean noop; only
    # ``error`` (full failure) returns None.
    if getattr(run, "status", "error") == "error":
        return None
    return frappe.db.get_value(
        "Ecommerce Item",
        {
            "erpnext_item_code": item_code,
            "integration": "shopware6" if backend == BACKEND_SHOPWARE else backend.lower(),
        },
        "integration_item_code",
    )
