"""Unified per-Item resolver for Catalog Mirror + Smart Collections.

The resolver is the single source of truth for "which channels and
categories does Item X land on in backend Y?" Three layers contribute,
in precedence order:

1. **Per-Item overrides** (child table on Item — added by Phase 4).
   ``mode=include`` injects a channel; ``mode=exclude`` vetoes the
   channel for this item regardless of any other layer.

2. **Catalog Mirror.** Every active Mirror whose ``root_item_group`` is
   an ancestor of the Item's ``item_group`` contributes:

   - one channel entry with visibility=30 (mirrors are "fully visible"
     by design — the operator pinned this IG branch to a sales channel)
   - one category entry per IG along the ancestor chain, using the
     ``<backend>_category_id`` custom field written by ``tasks.py``.

3. **Smart Collections.** Every active Smart Collection whose resolver
   matches the Item contributes one entry per target row (channel,
   visibility, optional external_id).

**Variant A precedence (locked):** highest visibility wins per channel.
A Mirror's 30 cannot be weakened by a Smart Collection's 20. Only a
Per-Item Override with ``mode=exclude`` can suppress a channel.

The result is cached for 5 minutes per backend, mirroring the legacy
``smart_collections.channel_visibility`` index. Cache invalidation
hooks (Item / Item Group / Smart Collection save) call into
``invalidate_cache()`` here via the delegator in
``smart_collections.channel_visibility``.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass, field

import frappe

from ecommerce_integrations.catalog_mirror.constants import (
    BACKEND_MEDUSA,
    BACKEND_SHOPWARE,
    KNOWN_BACKENDS,
)

_MIRROR_DOCTYPE = "Ecommerce Catalog Mirror"
_SC_DOCTYPE = "Ecommerce Smart Collection"
_SC_TARGET_DOCTYPE = "Ecommerce Smart Collection Target"
_ITEM_OVERRIDE_FIELD = "ecommerce_channel_overrides"

# Visibility constants. Shopware semantics; Medusa ignores visibility but
# we still produce entries so the consumer code path stays uniform.
VISIBILITY_HIDDEN = 0
VISIBILITY_LINK = 10
VISIBILITY_SEARCH = 20
VISIBILITY_ALL = 30

# Mirrors are treated as "fully visible" — the operator pinned this IG
# branch to the channel, so weakening visibility there would be
# surprising. Smart Collections can opt into lower levels.
MIRROR_VISIBILITY = VISIBILITY_ALL

_CACHE_KEY = "catalog_mirror_resolver:{backend}"
_CACHE_TTL_SEC = 300


# ---------------------------------------------------------------------------
# Public dataclasses (LOCKED — Phase 4 builds against this contract)
# ---------------------------------------------------------------------------


@dataclass
class ChannelEntry:
    """One (sales_channel, visibility) decision for an Item × backend.

    ``source`` is a short string in the form ``"<layer>:<detail>"`` so
    the Item form indicator can render a human-readable origin and
    deep-link to ``source_doc`` (the contributing doc's name, when
    applicable).
    """

    sales_channel: str
    visibility: int
    source: str
    source_doc: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CategoryEntry:
    """One backend category placement for an Item × backend.

    Mirror sources outrank Smart Collection sources at the dedup step —
    Mirror placements are authoritative by construction.
    """

    external_id: str
    name: str | None
    source: str
    source_doc: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ItemResolution:
    """All channels + categories an Item resolves to for one backend.

    ``excluded_channels`` is informational: when the operator excludes a
    channel via per-item override, the entry shows up here AND vetoes
    the channel everywhere else in ``channels``. The form uses this to
    render the strike-through state for excluded entries.
    """

    item_code: str
    backend: str
    channels: list[ChannelEntry] = field(default_factory=list)
    categories: list[CategoryEntry] = field(default_factory=list)
    excluded_channels: list[ChannelEntry] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "item_code": self.item_code,
            "backend": self.backend,
            "channels": [c.to_dict() for c in self.channels],
            "categories": [c.to_dict() for c in self.categories],
            "excluded_channels": [c.to_dict() for c in self.excluded_channels],
            "warnings": list(self.warnings),
        }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def resolve_item(item_code: str, backend: str) -> ItemResolution:
    """Run the full resolution chain for one item and backend.

    Cached for 5 minutes per backend. Callers that need a fresh result
    after a write should call :func:`invalidate_cache` first.

    Fast path: when the per-backend index is fresh and the item is
    absent from it, the item has no contribution from any of the
    three layers (per-item override, Catalog Mirror, Smart
    Collection) — return an empty resolution without the 3 SQL
    queries ``_resolve_uncached`` would otherwise issue. At 30k+
    items with sparse layer coverage (typical for Medusa Syncs
    where only a few Smart Collections cover a subset of the
    catalogue) this collapses ~90k differ-phase SQL queries to
    zero, restoring parity with the Shopware-side differ speed
    where most items hit the index.

    Slow path falls through to ``_resolve_uncached`` for items
    added between cache builds (5-min TTL). New Items typically
    arrive via doc-event hooks that re-invalidate the index, so
    in steady-state operation this branch is rare.
    """
    if backend not in KNOWN_BACKENDS:
        raise ValueError(
            f"Unknown backend {backend!r}; expected one of {KNOWN_BACKENDS}"
        )
    index = _index_for_backend(backend)
    cached = index.get(item_code)
    if cached is not None:
        return cached
    # Fast path: absent from index → no layer touches this item →
    # synthesise an empty resolution. Callers (notably the engine's
    # ``_canonical_visibilities``) fall back to their sync-aware
    # default-channel logic when the resolver yields no entries.
    return ItemResolution(item_code=item_code, backend=backend)


def channels_for_item(item_code: str, backend: str) -> list[ChannelEntry]:
    """Return just the channel entries (after dedup + veto + fallback)."""
    return list(resolve_item(item_code, backend).channels)


def categories_for_item(item_code: str, backend: str) -> list[CategoryEntry]:
    """Return just the category entries (after dedup)."""
    return list(resolve_item(item_code, backend).categories)


def invalidate_cache(backend: str | None = None) -> None:
    """Drop the cached per-backend index.

    ``backend=None`` invalidates both backends. Called from the doc
    event hooks that already cover Item / Item Group / Smart Collection
    saves via the delegator in ``smart_collections.channel_visibility``.
    """
    backends = (backend,) if backend else KNOWN_BACKENDS
    for b in backends:
        try:
            frappe.cache.delete_key(_CACHE_KEY.format(backend=b))
        except Exception:
            # Cache backends in dev may not be initialised; do not let
            # invalidate failures bubble up and break the originating
            # save.
            pass


# ---------------------------------------------------------------------------
# Cache plumbing
# ---------------------------------------------------------------------------


def _index_for_backend(backend: str) -> dict[str, ItemResolution]:
    """Return ``{item_code: ItemResolution}`` for every item the layers
    contribute to. Items absent from the index get resolved on demand
    by :func:`_resolve_uncached`."""
    key = _CACHE_KEY.format(backend=backend)
    try:
        cached = frappe.cache.get_value(key)
    except Exception:
        cached = None
    if cached is not None:
        return cached
    index = _build_index(backend)
    try:
        frappe.cache.set_value(key, index, expires_in_sec=_CACHE_TTL_SEC)
    except Exception:
        pass
    return index


def _build_index(backend: str) -> dict[str, ItemResolution]:
    """Build the full per-item index for one backend.

    Aggregates contributions from all three layers in a single pass —
    one SQL roundtrip per layer rather than N+1 per item. Items the
    layers don't touch are resolved lazily on demand.
    """
    overrides_by_item = _load_overrides_index()
    mirror_contribs = _load_mirror_contributions(backend)
    sc_contribs, sc_items = _load_sc_contributions(backend)

    all_items: set[str] = set()
    all_items.update(overrides_by_item.keys())
    all_items.update(mirror_contribs.keys())
    all_items.update(sc_items)

    index: dict[str, ItemResolution] = {}
    for item_code in all_items:
        resolution = _assemble_resolution(
            item_code,
            backend,
            overrides=overrides_by_item.get(item_code, []),
            mirror_entries=mirror_contribs.get(item_code, ([], [])),
            sc_entries=sc_contribs.get(item_code, ([], [])),
        )
        index[item_code] = resolution
    return index


def _resolve_uncached(item_code: str, backend: str) -> ItemResolution:
    """Fall-through resolution for items not present in the cached index.

    Called for ad-hoc lookups (e.g. a brand-new Item the cache hasn't
    been rebuilt for yet). Reads the per-item layer for this item only
    and reuses the cached mirror / SC contributions if present.
    """
    overrides = _load_overrides_for_item(item_code)
    mirror_entries = _resolve_mirror_for_item(item_code, backend)
    sc_entries = _resolve_sc_for_item(item_code, backend)
    return _assemble_resolution(
        item_code,
        backend,
        overrides=overrides,
        mirror_entries=mirror_entries,
        sc_entries=sc_entries,
    )


# ---------------------------------------------------------------------------
# Layer 1: per-item overrides
# ---------------------------------------------------------------------------


def _override_field_exists() -> bool:
    """Return True when the Phase 4 ``ecommerce_channel_overrides``
    child table is installed.

    Phase 4 wires the field on Item. Until then, this returns False and
    the override layer is a no-op — Mirror + SC handle everything.
    """
    return bool(
        frappe.db.exists(
            "Custom Field",
            {"dt": "Item", "fieldname": _ITEM_OVERRIDE_FIELD},
        )
    )


def _override_child_doctype() -> str | None:
    """Resolve the child doctype that the override field points at.

    Phase 4 picks the doctype name; we resolve at runtime so the
    resolver doesn't hardcode a string that may not exist yet.
    """
    row = frappe.db.get_value(
        "Custom Field",
        {"dt": "Item", "fieldname": _ITEM_OVERRIDE_FIELD},
        ["options", "fieldtype"],
        as_dict=True,
    )
    if not row or row.fieldtype != "Table":
        return None
    return row.options or None


def _load_overrides_index() -> dict[str, list[dict]]:
    """Return ``{item_code: [override_row_dict, ...]}`` from the child
    table, or an empty dict if Phase 4 hasn't installed the field yet."""
    if not _override_field_exists():
        return {}
    child = _override_child_doctype()
    if not child:
        return {}
    rows = frappe.db.sql(
        f"""
        SELECT parent AS item_code, sales_channel, visibility, mode, backend
        FROM `tab{child}`
        WHERE parenttype = 'Item'
          AND parentfield = %s
        """,
        (_ITEM_OVERRIDE_FIELD,),
        as_dict=True,
    )
    grouped: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        grouped[r["item_code"]].append(dict(r))
    return dict(grouped)


def _load_overrides_for_item(item_code: str) -> list[dict]:
    """Per-item override fetch for the ad-hoc lookup path."""
    if not _override_field_exists():
        return []
    child = _override_child_doctype()
    if not child:
        return []
    rows = frappe.db.sql(
        f"""
        SELECT sales_channel, visibility, mode, backend
        FROM `tab{child}`
        WHERE parent = %s
          AND parenttype = 'Item'
          AND parentfield = %s
        """,
        (item_code, _ITEM_OVERRIDE_FIELD),
        as_dict=True,
    )
    return [dict(r) for r in rows]


def _build_override_entries(
    overrides: list[dict],
    backend: str,
) -> tuple[list[ChannelEntry], list[ChannelEntry], set[str]]:
    """Translate the raw rows into include/exclude lists + veto set.

    Returns ``(include_entries, exclude_entries, vetoed_channels)``.
    ``vetoed_channels`` is the set of channel ids the operator
    explicitly excluded — later layers must skip them for THIS item.
    Rows without a ``backend`` column or with a mismatching backend are
    ignored (the override table is intentionally backend-aware so the
    same row table can carry rules for both Shopware and Medusa).
    """
    includes: list[ChannelEntry] = []
    excludes: list[ChannelEntry] = []
    vetoed: set[str] = set()
    for row in overrides:
        row_backend = (row.get("backend") or "").strip()
        if row_backend and row_backend != backend:
            continue
        sc = (row.get("sales_channel") or "").strip()
        if not sc:
            continue
        mode = (row.get("mode") or "include").strip().lower()
        vis = _parse_visibility_int(row.get("visibility"))
        if mode == "exclude":
            excludes.append(
                ChannelEntry(
                    sales_channel=sc,
                    visibility=VISIBILITY_HIDDEN,
                    source="override:exclude",
                    source_doc=None,
                )
            )
            vetoed.add(sc)
        else:
            includes.append(
                ChannelEntry(
                    sales_channel=sc,
                    visibility=vis if vis > 0 else VISIBILITY_ALL,
                    source="override:include",
                    source_doc=None,
                )
            )
    return includes, excludes, vetoed


# ---------------------------------------------------------------------------
# Layer 2: Catalog Mirror
# ---------------------------------------------------------------------------


def _mapping_field(backend: str) -> str | None:
    if backend == BACKEND_SHOPWARE:
        return "shopware_category_id"
    if backend == BACKEND_MEDUSA:
        return "medusa_category_id"
    return None


def _ig_ancestors(item_group: str) -> list[str]:
    """Return ``[item_group, parent, grandparent, ...]`` via nested-set.

    Uses ``lft`` / ``rgt`` so the lookup is a single SQL query rather
    than a recursive walk. Order is leaf-first; the caller iterates and
    matches against mirror roots in that order.
    """
    if not item_group:
        return []
    row = frappe.db.get_value(
        "Item Group", item_group, ["lft", "rgt"], as_dict=True,
    )
    if not row or row.lft is None:
        return [item_group]
    rows = frappe.db.sql(
        """
        SELECT name FROM `tabItem Group`
        WHERE lft <= %s AND rgt >= %s
        ORDER BY lft DESC
        """,
        (row.lft, row.rgt),
        as_dict=True,
    )
    return [r["name"] for r in rows]


def _load_mirror_contributions(
    backend: str,
) -> dict[str, tuple[list[ChannelEntry], list[CategoryEntry]]]:
    """Build the per-item mirror contributions for the index.

    Strategy: for each active Mirror, find every Item whose
    ``item_group`` sits in the Mirror's root subtree (single SQL
    join via lft/rgt), then for each such item emit one ChannelEntry
    and the ancestor chain of CategoryEntries.
    """
    mirrors = frappe.get_all(
        _MIRROR_DOCTYPE,
        filters={"is_active": 1, "backend": backend},
        fields=[
            "name",
            "title",
            "target_sales_channel",
            "root_item_group",
        ],
    )
    if not mirrors:
        return {}

    field = _mapping_field(backend)
    out: dict[str, tuple[list[ChannelEntry], list[CategoryEntry]]] = defaultdict(
        lambda: ([], [])
    )

    for mirror in mirrors:
        if not mirror.root_item_group:
            continue
        items = _items_under_root(mirror.root_item_group)
        if not items:
            continue
        category_chain_cache: dict[str, list[CategoryEntry]] = {}
        for row in items:
            item_code = row["item_code"]
            ig = row["item_group"]
            chain = category_chain_cache.get(ig)
            if chain is None:
                chain = _category_chain_for_ig(
                    ig, mirror.root_item_group, field, mirror.name,
                )
                category_chain_cache[ig] = chain
            ch_list, cat_list = out[item_code]
            if mirror.target_sales_channel:
                ch_list.append(
                    ChannelEntry(
                        sales_channel=mirror.target_sales_channel,
                        visibility=MIRROR_VISIBILITY,
                        source=f"mirror:{mirror.name}",
                        source_doc=mirror.name,
                    )
                )
            cat_list.extend(chain)
    return dict(out)


def _items_under_root(root_ig: str) -> list[dict]:
    """Return ``[{item_code, item_group}, ...]`` for every active Item
    whose item_group is in the nested-set subtree of ``root_ig``.

    Skips items whose item_group (or any ancestor IG) carries
    ``catalog_mirror_skip=1``.
    """
    root = frappe.db.get_value(
        "Item Group", root_ig, ["lft", "rgt"], as_dict=True,
    )
    if not root or root.lft is None:
        return []
    rows = frappe.db.sql(
        """
        SELECT i.name AS item_code, i.item_group AS item_group
        FROM `tabItem` i
        INNER JOIN `tabItem Group` g ON g.name = i.item_group
        WHERE g.lft >= %s AND g.rgt <= %s
          AND COALESCE(i.disabled, 0) = 0
        """,
        (root.lft, root.rgt),
        as_dict=True,
    )
    if not rows:
        return []
    skipped_groups = _skipped_groups_under(root.lft, root.rgt)
    return [r for r in rows if r["item_group"] not in skipped_groups]


def _skipped_groups_under(lft: int, rgt: int) -> set[str]:
    """Return the set of Item Group names in the subtree that have
    ``catalog_mirror_skip=1`` OR sit under a skipped ancestor.

    The custom field is added by ``patches/setup_catalog_mirror``; we
    guard for its absence so the resolver still works on sites that
    haven't migrated yet.
    """
    if not frappe.db.exists(
        "Custom Field",
        {"dt": "Item Group", "fieldname": "catalog_mirror_skip"},
    ):
        return set()
    rows = frappe.db.sql(
        """
        SELECT lft, rgt FROM `tabItem Group`
        WHERE lft >= %s AND rgt <= %s
          AND catalog_mirror_skip = 1
        """,
        (lft, rgt),
        as_dict=True,
    )
    if not rows:
        return set()
    skipped: set[str] = set()
    for r in rows:
        children = frappe.db.sql(
            """
            SELECT name FROM `tabItem Group`
            WHERE lft >= %s AND rgt <= %s
            """,
            (r["lft"], r["rgt"]),
            as_dict=True,
        )
        for c in children:
            skipped.add(c["name"])
    return skipped


def _category_chain_for_ig(
    ig: str,
    root_ig: str,
    field: str | None,
    mirror_name: str,
) -> list[CategoryEntry]:
    """Walk from ``ig`` up to ``root_ig`` and emit one CategoryEntry per
    Item Group that has a non-empty ``<backend>_category_id``.

    The chain is ordered leaf-first so the consumer sees the most
    specific category first. The root IG is included when it carries an
    external_id — operators occasionally use the root as the catch-all
    "Products" category.
    """
    if not field:
        return []
    ancestors = _ig_ancestors(ig)
    # Trim everything above root_ig so we don't leak categories from
    # unrelated branches when an IG happens to sit under multiple
    # potential roots (shouldn't happen on a single-rooted tree but the
    # safety guard is cheap).
    out: list[CategoryEntry] = []
    for a in ancestors:
        ext_id = frappe.db.get_value("Item Group", a, field)
        if not ext_id:
            if a == root_ig:
                break
            continue
        ig_name = frappe.db.get_value("Item Group", a, "item_group_name") or a
        out.append(
            CategoryEntry(
                external_id=ext_id,
                name=ig_name,
                source=f"mirror:{mirror_name}",
                source_doc=mirror_name,
            )
        )
        if a == root_ig:
            break
    return out


def _resolve_mirror_for_item(
    item_code: str, backend: str,
) -> tuple[list[ChannelEntry], list[CategoryEntry]]:
    """Mirror layer for the ad-hoc (uncached) lookup path.

    Reads the item's item_group, walks ancestors, and matches against
    every active Mirror for the backend. Slower than the batched index
    build but used only for items the cache hasn't seen.
    """
    ig = frappe.db.get_value("Item", item_code, "item_group")
    if not ig:
        return [], []
    # Skip when the item's IG (or any ancestor) is flagged
    # catalog_mirror_skip.
    if _is_ig_skipped(ig):
        return [], []
    ancestors = _ig_ancestors(ig)
    if not ancestors:
        return [], []
    ancestor_set = set(ancestors)

    mirrors = frappe.get_all(
        _MIRROR_DOCTYPE,
        filters={"is_active": 1, "backend": backend},
        fields=["name", "target_sales_channel", "root_item_group"],
    )
    field = _mapping_field(backend)
    channels: list[ChannelEntry] = []
    categories: list[CategoryEntry] = []
    for mirror in mirrors:
        if mirror.root_item_group not in ancestor_set:
            continue
        if mirror.target_sales_channel:
            channels.append(
                ChannelEntry(
                    sales_channel=mirror.target_sales_channel,
                    visibility=MIRROR_VISIBILITY,
                    source=f"mirror:{mirror.name}",
                    source_doc=mirror.name,
                )
            )
        categories.extend(
            _category_chain_for_ig(
                ig, mirror.root_item_group, field, mirror.name,
            )
        )
    return channels, categories


def _is_ig_skipped(ig: str) -> bool:
    """True when the IG or any ancestor has ``catalog_mirror_skip=1``."""
    if not frappe.db.exists(
        "Custom Field",
        {"dt": "Item Group", "fieldname": "catalog_mirror_skip"},
    ):
        return False
    for a in _ig_ancestors(ig):
        if frappe.db.get_value("Item Group", a, "catalog_mirror_skip"):
            return True
    return False


# ---------------------------------------------------------------------------
# Layer 3: Smart Collections
# ---------------------------------------------------------------------------


def _load_sc_contributions(
    backend: str,
) -> tuple[
    dict[str, tuple[list[ChannelEntry], list[CategoryEntry]]],
    set[str],
]:
    """Resolve every active SC's items in one pass.

    Returns ``(contributions, all_matched_items)``. The ``all_matched_items``
    set is the union of matches across collections — used by
    :func:`_build_index` so items that match an SC but no other layer
    still get an ItemResolution entry.
    """
    from ecommerce_integrations.smart_collections.engine.resolver import resolve

    rows = frappe.db.sql(
        """
        SELECT sc.name AS collection, sc.title AS title,
               t.sales_channel, t.visibility, t.external_id
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

    by_collection: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_collection[r["collection"]].append(r)

    contributions: dict[
        str, tuple[list[ChannelEntry], list[CategoryEntry]]
    ] = defaultdict(lambda: ([], []))
    all_items: set[str] = set()

    for coll_name, targets in by_collection.items():
        coll = frappe.get_doc(_SC_DOCTYPE, coll_name)
        items = resolve(coll, persist_stats=False)
        all_items.update(items)
        for item_code in items:
            ch_list, cat_list = contributions[item_code]
            for t in targets:
                vis = _parse_visibility_int(t["visibility"])
                ch_list.append(
                    ChannelEntry(
                        sales_channel=t["sales_channel"],
                        visibility=vis,
                        source=f"smart_collection:{coll_name}",
                        source_doc=coll_name,
                    )
                )
                if t.get("external_id"):
                    cat_list.append(
                        CategoryEntry(
                            external_id=t["external_id"],
                            name=None,
                            source=f"smart_collection:{coll_name}",
                            source_doc=coll_name,
                        )
                    )
    return dict(contributions), all_items


def _resolve_sc_for_item(
    item_code: str, backend: str,
) -> tuple[list[ChannelEntry], list[CategoryEntry]]:
    """SC layer for the ad-hoc (uncached) lookup path."""
    from ecommerce_integrations.smart_collections.engine.resolver import resolve

    rows = frappe.db.sql(
        """
        SELECT sc.name AS collection, sc.title AS title,
               t.sales_channel, t.visibility, t.external_id
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
    by_collection: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_collection[r["collection"]].append(r)

    channels: list[ChannelEntry] = []
    categories: list[CategoryEntry] = []
    for coll_name, targets in by_collection.items():
        coll = frappe.get_doc(_SC_DOCTYPE, coll_name)
        items = resolve(coll, persist_stats=False)
        if item_code not in items:
            continue
        for t in targets:
            vis = _parse_visibility_int(t["visibility"])
            channels.append(
                ChannelEntry(
                    sales_channel=t["sales_channel"],
                    visibility=vis,
                    source=f"smart_collection:{coll_name}",
                    source_doc=coll_name,
                )
            )
            if t.get("external_id"):
                categories.append(
                    CategoryEntry(
                        external_id=t["external_id"],
                        name=None,
                        source=f"smart_collection:{coll_name}",
                        source_doc=coll_name,
                    )
                )
    return channels, categories


# ---------------------------------------------------------------------------
# Assembly: dedup, veto, fallback
# ---------------------------------------------------------------------------


def _assemble_resolution(
    item_code: str,
    backend: str,
    *,
    overrides: list[dict],
    mirror_entries: tuple[list[ChannelEntry], list[CategoryEntry]],
    sc_entries: tuple[list[ChannelEntry], list[CategoryEntry]],
) -> ItemResolution:
    """Combine the three layers into one ItemResolution.

    Dedup rules (LOCKED):

    - Channels: keep the highest-visibility entry per sales_channel.
      On a tie, prefer override > mirror > smart_collection (the
      ``_source_rank`` ordering below).
    - Categories: dedup by external_id; prefer mirror sources over
      smart_collection sources.
    """
    include_entries, exclude_entries, vetoed = _build_override_entries(
        overrides, backend
    )
    mirror_ch, mirror_cat = mirror_entries
    sc_ch, sc_cat = sc_entries

    # Apply the exclude-veto BEFORE dedup so vetoed channels never even
    # enter the candidate pool.
    candidates: list[ChannelEntry] = []
    candidates.extend(include_entries)
    candidates.extend(e for e in mirror_ch if e.sales_channel not in vetoed)
    candidates.extend(e for e in sc_ch if e.sales_channel not in vetoed)

    # Dedup by sales_channel: highest visibility wins; on a tie, prefer
    # the higher-ranked source (override > mirror > smart_collection).
    best: dict[str, ChannelEntry] = {}
    for entry in candidates:
        if entry.visibility <= VISIBILITY_HIDDEN:
            continue
        cur = best.get(entry.sales_channel)
        if cur is None or _channel_outranks(entry, cur):
            best[entry.sales_channel] = entry

    channels = list(best.values())

    # Dedup categories by external_id, preferring mirror sources.
    cat_best: dict[str, CategoryEntry] = {}
    for cat in list(mirror_cat) + list(sc_cat):
        if not cat.external_id:
            continue
        cur = cat_best.get(cat.external_id)
        if cur is None or _category_outranks(cat, cur):
            cat_best[cat.external_id] = cat
    categories = list(cat_best.values())

    warnings: list[str] = []
    if not channels:
        fallback = _default_channel_fallback(backend)
        if fallback is not None:
            channels.append(fallback)
            warnings.append(
                "no Mirror matches and no overrides — fallback to default channel"
            )
        elif backend == BACKEND_SHOPWARE:
            warnings.append(
                "no Mirror, SC or override match and no default channel configured"
            )
        # Medusa intentionally has no fallback — Medusa exports without
        # channel restriction so an empty channel list is the correct
        # signal upstream.

    return ItemResolution(
        item_code=item_code,
        backend=backend,
        channels=channels,
        categories=categories,
        excluded_channels=exclude_entries,
        warnings=warnings,
    )


_SOURCE_RANK = {
    "override": 3,
    "mirror": 2,
    "smart_collection": 1,
    "default": 0,
}


def _source_rank(source: str) -> int:
    head = (source or "").split(":", 1)[0]
    return _SOURCE_RANK.get(head, 0)


def _channel_outranks(new: ChannelEntry, cur: ChannelEntry) -> bool:
    """Variant A: highest visibility wins; ties go to higher source rank."""
    if new.visibility != cur.visibility:
        return new.visibility > cur.visibility
    return _source_rank(new.source) > _source_rank(cur.source)


def _category_outranks(new: CategoryEntry, cur: CategoryEntry) -> bool:
    """Mirror sources outrank smart_collection sources at category dedup."""
    return _source_rank(new.source) > _source_rank(cur.source)


def _default_channel_fallback(backend: str) -> ChannelEntry | None:
    """Return the configured default channel as a ChannelEntry, or None.

    Shopware: reads ``Shopware Setting.sales_channels[is_default=1]``.
    Medusa: no equivalent (Medusa exports without per-channel scoping).
    """
    if backend != BACKEND_SHOPWARE:
        return None
    if not frappe.db.exists("DocType", "Shopware Setting"):
        return None
    try:
        setting = frappe.get_cached_doc("Shopware Setting", "Shopware Setting")
    except Exception:
        return None
    default_id = None
    for sc in setting.sales_channels or []:
        if getattr(sc, "is_default", 0) and getattr(sc, "active", 0):
            default_id = sc.sales_channel_id
            break
    if not default_id:
        for sc in setting.sales_channels or []:
            if getattr(sc, "active", 0):
                default_id = sc.sales_channel_id
                break
    if not default_id:
        return None
    return ChannelEntry(
        sales_channel=default_id,
        visibility=VISIBILITY_ALL,
        source="default",
        source_doc=None,
    )


# ---------------------------------------------------------------------------
# Visibility parsing
# ---------------------------------------------------------------------------


def _parse_visibility_int(value) -> int:
    """Coerce visibility values to the Shopware integer scale.

    Accepts the Smart Collections label form ("All (30)" / "Linked
    Only (20)" / …) as well as raw ints/strings. Unknown labels fall
    back to ``VISIBILITY_ALL`` so a misconfigured target still produces
    a sensible default rather than silently hiding the product.
    """
    if value is None:
        return VISIBILITY_ALL
    if isinstance(value, int):
        return value
    s = str(value).strip()
    if not s:
        return VISIBILITY_ALL
    # Direct integer parse first.
    try:
        return int(s)
    except ValueError:
        pass
    low = s.lower()
    if "30" in s or "all" in low:
        return VISIBILITY_ALL
    if "20" in s or "search" in low or "linked only" in low:
        return VISIBILITY_SEARCH
    if "10" in s or "link" in low:
        return VISIBILITY_LINK
    if "0" in s or "hidden" in low or "exclude" in low:
        return VISIBILITY_HIDDEN
    return VISIBILITY_ALL
