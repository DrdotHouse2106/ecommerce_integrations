"""Doc-event hooks for Smart Collections.

Each hook drops the per-backend visibility cache so the next product-sync
cycle (or the next ``channels_for_item`` lookup) re-resolves against the
current ERPNext state. We intentionally do *not* enqueue an immediate
backend sync from these hooks — that would push back-pressure into the
user's save action. The hourly scheduled job and the manual *Sync Now*
button are the actual sync drivers.
"""

import frappe

from ecommerce_integrations.smart_collections.channel_visibility import (
    invalidate_cache,
)


def invalidate_visibility_cache(doc, method=None) -> None:
    # ``method`` is supplied by Frappe doc-event hooks; ignored here.
    try:
        invalidate_cache()
    except Exception as e:
        # Cache invalidation must never break the originating save.
        frappe.logger("smart_collections").debug(
            f"invalidate_cache failed: {type(e).__name__}: {e}"
        )
