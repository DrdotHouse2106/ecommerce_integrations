"""Smart-Collections-driven channel visibility lookup.

Replaces the legacy ``Setting.get_channels_for_item(item_group, brand)``
indirection used by the Medusa and Shopware product-sync code paths. The
new logic asks the Smart Collections engine "for the given item, which
channels of the given backend should it appear in?" and returns one entry
per (collection × target) the item resolves into — with the target's
configured visibility level so Shopware's salesChannels association can
distinguish ``All (30)`` vs ``Linked Only (20)`` etc.

The reverse index (item_code → channels) is cached in Frappe's cache for
five minutes; cache keys are namespaced by backend so changes to one
backend's collections don't drop the other backend's index. Hooks
invalidate on Item / Item Ecommerce Property / Item Group save.
"""

from collections import defaultdict

import frappe

from ecommerce_integrations.smart_collections.constants import (
    KNOWN_BACKENDS,
    VISIBILITY_DEFAULT,
)
from ecommerce_integrations.smart_collections.engine.resolver import resolve


_CACHE_KEY_PREFIX = "smart_collections_visibility_index:"
_CACHE_TTL_SEC = 300


def channels_for_item(item_code: str, backend: str) -> list[dict]:
    """Return all (sales_channel, visibility) entries the item resolves into.

    Each entry is a dict with ``sales_channel``, ``visibility`` and the
    matching ``collection`` name. Multiple Smart Collections targeting the
    same channel produce multiple entries — callers that just need a
    deduped channel list should pass through ``unique_channel_ids``.
    """
    index = _index_for_backend(backend)
    return list(index.get(item_code, ()))


def unique_channel_ids(entries: list[dict]) -> list[str]:
    """Deduplicate ``channels_for_item`` output down to channel ids."""
    seen: list[str] = []
    seen_set: set[str] = set()
    for e in entries:
        ch = e["sales_channel"]
        if ch in seen_set:
            continue
        seen_set.add(ch)
        seen.append(ch)
    return seen


def invalidate_cache(backend: str | None = None) -> None:
    """Drop the cached reverse index. ``backend=None`` drops both."""
    backends = (backend,) if backend else KNOWN_BACKENDS
    for b in backends:
        frappe.cache.delete_key(_CACHE_KEY_PREFIX + b)


def _index_for_backend(backend: str) -> dict[str, list[dict]]:
    cache_key = _CACHE_KEY_PREFIX + backend
    cached = frappe.cache.get_value(cache_key)
    if cached is not None:
        return cached
    index = _build_index(backend)
    frappe.cache.set_value(cache_key, index, expires_in_sec=_CACHE_TTL_SEC)
    return index


def _build_index(backend: str) -> dict[str, list[dict]]:
    targets = frappe.db.sql(
        """
        SELECT sc.name AS collection, sc.title,
               t.sales_channel, t.visibility
        FROM `tabEcommerce Smart Collection` sc
        INNER JOIN `tabEcommerce Smart Collection Target` t
            ON t.parent = sc.name
            AND t.parenttype = 'Ecommerce Smart Collection'
        WHERE sc.is_active = 1
          AND t.enabled = 1
          AND t.backend = %s
        """,
        (backend,),
        as_dict=True,
    )

    index: dict[str, list[dict]] = defaultdict(list)
    seen_per_item: dict[str, set[tuple[str, str]]] = defaultdict(set)

    # Resolving once per collection keeps the cost at one SQL query per
    # active collection rather than one per item — critical when the
    # product sync iterates over thousands of items.
    by_collection: dict[str, list[dict]] = defaultdict(list)
    for row in targets:
        by_collection[row.collection].append(row)

    for coll_name, rows in by_collection.items():
        coll = frappe.get_doc("Ecommerce Smart Collection", coll_name)
        items = resolve(coll, persist_stats=False)
        for item_code in items:
            for row in rows:
                vis = row.visibility or VISIBILITY_DEFAULT
                key = (row.sales_channel, vis)
                if key in seen_per_item[item_code]:
                    continue
                seen_per_item[item_code].add(key)
                index[item_code].append(
                    {
                        "sales_channel": row.sales_channel,
                        "visibility": vis,
                        "collection": coll_name,
                    }
                )
    return dict(index)
