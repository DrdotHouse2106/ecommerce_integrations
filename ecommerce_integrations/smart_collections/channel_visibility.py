"""Smart-Collections-driven channel visibility lookup (DEPRECATED).

Phase 2 of the Catalog Mirror project moved the canonical
"which channels does Item X land on?" logic into
:mod:`ecommerce_integrations.catalog_mirror.resolver`, which combines
per-item overrides, Catalog Mirror placements and Smart Collections in
one place with **Variant A** precedence (highest visibility wins).

This module is kept for **backwards compatibility only** — every public
symbol now delegates to the new resolver. The legacy `_build_index`
helper is retained as dead code; it's no longer wired up and exists
solely so a downstream import doesn't break on a half-rolled-out site.

New code should import from
``ecommerce_integrations.catalog_mirror.resolver`` directly.
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
    """Return legacy-shape entries via the unified resolver.

    DEPRECATED — delegates to
    :func:`ecommerce_integrations.catalog_mirror.resolver.channels_for_item`
    and reshapes the result to the legacy dict form
    (``{sales_channel, visibility, collection}``) so existing callers
    keep working. New code should call the resolver directly to get the
    typed :class:`ChannelEntry` objects with full source info.
    """
    from ecommerce_integrations.catalog_mirror.resolver import (
        channels_for_item as _new_channels_for_item,
    )

    entries = _new_channels_for_item(item_code, backend)
    return [
        {
            "sales_channel": e.sales_channel,
            "visibility": e.visibility,
            "collection": e.source_doc,
        }
        for e in entries
    ]


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
    """Drop the cached reverse index (both legacy and resolver).

    Hooks call this function (via
    :func:`smart_collections.hooks.invalidate_visibility_cache`) on
    Item / Item Group / Smart Collection save. The legacy cache key is
    flushed for safety even though no code path reads it any more —
    cheap to clear, and keeps a half-rolled-out site honest.
    """
    from ecommerce_integrations.catalog_mirror.resolver import (
        invalidate_cache as _new_invalidate_cache,
    )

    _new_invalidate_cache(backend)

    backends = (backend,) if backend else KNOWN_BACKENDS
    for b in backends:
        try:
            frappe.cache.delete_key(_CACHE_KEY_PREFIX + b)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Dead code (kept for backwards compat — never called)
# ---------------------------------------------------------------------------


def _index_for_backend(backend: str) -> dict[str, list[dict]]:
    """Legacy reverse-index lookup. Retained as dead code."""
    cache_key = _CACHE_KEY_PREFIX + backend
    cached = frappe.cache.get_value(cache_key)
    if cached is not None:
        return cached
    index = _build_index(backend)
    frappe.cache.set_value(cache_key, index, expires_in_sec=_CACHE_TTL_SEC)
    return index


def _build_index(backend: str) -> dict[str, list[dict]]:
    """Legacy index builder. Retained as dead code; new code should use
    :mod:`ecommerce_integrations.catalog_mirror.resolver` instead."""
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
