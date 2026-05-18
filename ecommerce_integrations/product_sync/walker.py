"""Iterate over the Items that a Product Sync should consider.

The walker is the only piece of the Product Sync pipeline that talks to
``tabItem`` directly. Everything downstream (resolver, differ, adapters,
orchestrator) consumes :class:`ItemNode`.

The walker is an iterator on purpose: a Sync with ``scope='All'`` on a
large catalogue would otherwise materialise tens of thousands of rows
before the differ can start. Streaming keeps memory flat and lets the
caller short-circuit (e.g. cancel-flag checks between rows).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

import frappe

from ecommerce_integrations.product_sync.constants import (
    SCOPE_ALL,
    SCOPE_CATALOG_MIRROR,
    SCOPE_CUSTOM_FILTER,
    SCOPE_ITEM_GROUP,
    SCOPE_SMART_COLLECTION,
)


@dataclass
class ItemNode:
    """One Item considered by a Product Sync run.

    Attributes mirror ``tabItem`` 1:1 plus two derived booleans
    (``is_template``, ``is_variant``) so the differ and adapters don't
    have to re-check the same conditions for every product.
    """

    item_code: str
    item_name: str
    item_group: str | None
    brand: str | None
    has_variants: bool
    is_template: bool  # has_variants == 1
    is_variant: bool  # variant_of is not None
    variant_of: str | None
    standard_rate: float
    disabled: bool


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def walk_items_for_sync(sync_doc) -> Iterator[ItemNode]:
    """Yield every :class:`ItemNode` matched by ``sync_doc``'s scope.

    Dispatches on ``sync_doc.scope_mode``; each branch performs a single
    indexed SELECT against ``tabItem`` (joined with ``tabItem Group`` for
    the subtree variants) and streams rows. Big catalogues never get
    fully materialised — the caller decides when to stop iterating.

    Phase 1 covers the two scopes the form ships with day one (Catalog
    Mirror and Item Group subtree) plus ``All``. The Smart Collection
    and Custom Filter scopes are wired up as stubs returning nothing
    so the form's preview button doesn't crash when an operator picks
    one of them before Phase 2 lands.
    """
    scope = (getattr(sync_doc, "scope_mode", "") or "").strip()

    if scope == SCOPE_CATALOG_MIRROR:
        yield from _walk_via_catalog_mirror(sync_doc)
        return
    if scope == SCOPE_ITEM_GROUP:
        yield from _walk_via_item_group(sync_doc)
        return
    if scope == SCOPE_SMART_COLLECTION:
        yield from _walk_via_smart_collections(sync_doc)
        return
    if scope == SCOPE_CUSTOM_FILTER:
        # See note above; same fail-loud principle. Custom Filter
        # rules need a parser translating rows into tabItem WHERE
        # clauses — not yet implemented.
        frappe.log_error(
            title="Product Sync: Custom Filter scope not implemented",
            message=(
                f"Sync '{sync_doc.name}' uses scope_mode='Custom Filter' "
                "which has no walker implementation. Pick a different scope."
            ),
        )
        return
    if scope == SCOPE_ALL:
        yield from _walk_all_items(sync_doc)
        return

    # Unknown scope value — defensively yield nothing rather than raise,
    # so a misconfigured doc surfaces as "0 items in scope" in the
    # preview instead of an obscure 500.
    return


# ---------------------------------------------------------------------------
# Scope dispatch
# ---------------------------------------------------------------------------


def _walk_via_catalog_mirror(sync_doc) -> Iterator[ItemNode]:
    """Yield items in the linked Catalog Mirror's root subtree.

    Reads the linked Mirror's ``root_item_group``, walks the IG tree via
    the Catalog Mirror walker (so ``catalog_mirror_skip`` is honoured),
    and emits every Item whose ``item_group`` lands in that tree.
    """
    # Field on the Sync doc is ``linked_catalog_mirror`` (see the
    # Ecommerce Product Sync JSON). The walker historically read
    # ``catalog_mirror`` which always returned None — that swallowed
    # the entire Catalog-Mirror-scoped branch and made the preview
    # report "0 items in scope" even when the linked Mirror covered
    # thousands.
    mirror_name = (
        getattr(sync_doc, "linked_catalog_mirror", None)
        or getattr(sync_doc, "catalog_mirror", None)
    )
    if not mirror_name:
        return
    if not frappe.db.exists("Ecommerce Catalog Mirror", mirror_name):
        return

    root_ig = frappe.db.get_value(
        "Ecommerce Catalog Mirror", mirror_name, "root_item_group"
    )
    if not root_ig:
        return

    # Lazy import — Catalog Mirror is a sibling module; importing it at
    # module load time would create a needless dependency edge when the
    # walker is called in a Sync that doesn't use the Mirror scope.
    from ecommerce_integrations.catalog_mirror.walker import walk_erpnext_tree

    try:
        tree = walk_erpnext_tree(root_ig, include_inactive_leaves=True)
    except ValueError:
        # Root IG disappeared between the get_value above and here.
        return

    ig_names = {node.name for node in tree.iter_descendants()}
    if not ig_names:
        return

    yield from _select_items(sync_doc, ig_names=ig_names)


def _walk_via_item_group(sync_doc) -> Iterator[ItemNode]:
    """Yield items in ``sync_doc.root_item_group`` (+ descendants).

    Honours ``include_descendants``: when 0, only direct members of the
    root IG are emitted; when 1 (default), the entire nested-set subtree
    is included via a single ``lft``/``rgt`` join.
    """
    root_ig = getattr(sync_doc, "root_item_group", None)
    if not root_ig:
        return
    if not frappe.db.exists("Item Group", root_ig):
        return

    include_descendants = int(getattr(sync_doc, "include_descendants", 1) or 0)

    if include_descendants:
        row = frappe.db.get_value(
            "Item Group", root_ig, ["lft", "rgt"], as_dict=True
        )
        if not row or row.lft is None:
            return
        yield from _select_items(
            sync_doc, lft_rgt=(row.lft, row.rgt)
        )
    else:
        yield from _select_items(sync_doc, ig_names={root_ig})


def _walk_via_smart_collections(sync_doc) -> Iterator[ItemNode]:
    """Yield items resolved by one or more linked Smart Collections.

    Reads ``linked_smart_collections`` (child-table, multi-row) plus
    the legacy single ``linked_smart_collection`` link for back-compat.
    Each child row may carry an optional ``sales_channel_id`` filter:
    when set, the collection's items are only emitted if the Sync
    actually targets that channel (i.e. the channel id appears in
    ``target_sales_channels``). Empty ``sales_channel_id`` = the
    collection contributes to every channel of this Sync.

    The union of all matching collections' item-codes is emitted —
    duplicates are deduped here so the differ doesn't classify the
    same Item twice.
    """
    rows: list = list(getattr(sync_doc, "linked_smart_collections", []) or [])
    legacy_name = getattr(sync_doc, "linked_smart_collection", None)
    if legacy_name and not any(
        r.smart_collection == legacy_name for r in rows
    ):
        # Legacy single-link still wins when the new table is empty,
        # so installs that never edited the form after the migration
        # don't suddenly see an empty scope.
        class _Row:
            smart_collection = legacy_name
            sales_channel_id = None
        rows.append(_Row())

    if not rows:
        return

    # Build the target-channel set once for the channel-filter check.
    sync_channels = {
        (ch.sales_channel_id or "").strip()
        for ch in (getattr(sync_doc, "target_sales_channels", None) or [])
        if (ch.sales_channel_id or "").strip()
    }

    try:
        from ecommerce_integrations.smart_collections.engine.resolver import resolve
    except Exception as exc:  # noqa: BLE001 — module missing on minimal installs
        frappe.log_error(
            title="Product Sync: Smart Collection resolver unavailable",
            message=f"Sync '{sync_doc.name}': {exc}",
        )
        return

    matched_codes: set[str] = set()
    for row in rows:
        sc_name = getattr(row, "smart_collection", None)
        if not sc_name or not frappe.db.exists("Ecommerce Smart Collection", sc_name):
            continue
        row_channel = (getattr(row, "sales_channel_id", "") or "").strip()
        if row_channel and sync_channels and row_channel not in sync_channels:
            # Row-level channel restriction that doesn't match any of
            # this Sync's targets. Skip rather than emit "would never
            # be pushed anywhere" items.
            continue
        try:
            collection = frappe.get_cached_doc(
                "Ecommerce Smart Collection", sc_name,
            )
        except frappe.DoesNotExistError:
            continue
        try:
            matched_codes.update(resolve(collection, persist_stats=False))
        except Exception as exc:  # noqa: BLE001
            frappe.log_error(
                title=f"Product Sync: resolve('{sc_name}') failed",
                message=str(exc),
            )
            continue

    if not matched_codes:
        return

    yield from _select_items(
        sync_doc,
        item_codes=matched_codes,
    )


def _walk_all_items(sync_doc) -> Iterator[ItemNode]:
    """Yield every Item that survives the doc-level filters."""
    yield from _select_items(sync_doc)


# ---------------------------------------------------------------------------
# Item SELECT
# ---------------------------------------------------------------------------


def _select_items(
    sync_doc,
    *,
    ig_names: set[str] | None = None,
    lft_rgt: tuple[int, int] | None = None,
    item_codes: set[str] | None = None,
) -> Iterator[ItemNode]:
    """Run the streaming SELECT that backs every scope branch.

    Filters layered on top of the scope:

    - ``include_disabled`` (default 0) — WHERE ``disabled = 0``.
    - ``include_variants`` (default 1) — when 0, WHERE ``variant_of IS NULL``
      so the differ only sees templates / simple products.

    ``min_qty_in_stock`` is intentionally not implemented yet (Phase 5
    joins ``tabBin`` once the differ understands stock-aware diffs). It
    is documented here so the field on the master doctype matches a
    real code path.
    """
    include_disabled = int(getattr(sync_doc, "include_disabled", 0) or 0)
    include_variants = int(getattr(sync_doc, "include_variants", 1) or 0)
    # TODO(phase 5): wire ``min_qty_in_stock`` via a LEFT JOIN tabBin.
    # Until then the field is read-only on the form (master JSON sets
    # the input ``hidden=1``) so operators don't pick a value that the
    # walker silently ignores.

    where: list[str] = []
    params: dict = {}

    if not include_disabled:
        where.append("COALESCE(i.disabled, 0) = 0")
    if not include_variants:
        where.append("(i.variant_of IS NULL OR i.variant_of = '')")

    join = ""
    if lft_rgt is not None:
        join = "INNER JOIN `tabItem Group` g ON g.name = i.item_group"
        where.append("g.lft >= %(lft)s AND g.rgt <= %(rgt)s")
        params["lft"] = lft_rgt[0]
        params["rgt"] = lft_rgt[1]
    # ``item_codes`` filter is layered below — it composes with both
    # the IG-name filter (Catalog Mirror, Item Group subtree) and
    # the lft/rgt join, since callers may want to intersect a smart
    # collection with another scope dimension later.
    elif ig_names is not None:
        if not ig_names:
            return
        # frappe.db.sql safely interpolates a tuple-bound IN clause when
        # we pass the params dict; convert the set to a sorted tuple so
        # repeated calls produce identical query strings (query cache
        # friendliness).
        params["ig_names"] = tuple(sorted(ig_names))
        where.append("i.item_group IN %(ig_names)s")

    if item_codes is not None:
        if not item_codes:
            return
        # Same stability trick — sorted tuple gives a cache-friendly
        # query string across previews of the same Sync.
        params["item_codes"] = tuple(sorted(item_codes))
        where.append("i.name IN %(item_codes)s")

    where_clause = (" WHERE " + " AND ".join(where)) if where else ""

    rows = frappe.db.sql(
        f"""
        SELECT
            i.name AS item_code,
            i.item_name AS item_name,
            i.item_group AS item_group,
            i.brand AS brand,
            COALESCE(i.has_variants, 0) AS has_variants,
            i.variant_of AS variant_of,
            COALESCE(i.standard_rate, 0) AS standard_rate,
            COALESCE(i.disabled, 0) AS disabled
        FROM `tabItem` i
        {join}
        {where_clause}
        ORDER BY i.name ASC
        """,
        params,
        as_dict=True,
    )

    for row in rows:
        yield _row_to_node(row)


def _row_to_node(row: dict) -> ItemNode:
    """Translate one ``tabItem`` row into an :class:`ItemNode`.

    Centralised so a future column rename (e.g. ``standard_rate``) only
    has to be fixed in one place rather than across every scope branch.
    """
    has_variants = bool(int(row.get("has_variants") or 0))
    variant_of = row.get("variant_of") or None
    return ItemNode(
        item_code=row["item_code"],
        item_name=row.get("item_name") or row["item_code"],
        item_group=row.get("item_group") or None,
        brand=row.get("brand") or None,
        has_variants=has_variants,
        is_template=has_variants,
        is_variant=variant_of is not None,
        variant_of=variant_of,
        standard_rate=float(row.get("standard_rate") or 0.0),
        disabled=bool(int(row.get("disabled") or 0)),
    )
