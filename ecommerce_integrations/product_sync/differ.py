"""Product Sync differ — Item × Sync → ProductSyncPreviewPlan.

Backend-agnostic engine that decides what would happen for each Item
in a Sync's scope, without touching the backend in dry-run mode.

Decision tree per Item:

1. **Conflict** — Item belongs to a higher-priority Sync? → conflicts
   bucket, no further work. Resolver handles the rules (see
   :mod:`resolver`).
2. **No mapping** (no ``Ecommerce Item`` row for this backend) →
   ``create`` bucket.
3. **Mapping with stale external_id** (drift detected via adapter, the
   external id isn't in the live tree anymore) → ``mapping_drift``.
4. **Mapping with same hash** (``last_synced_hash`` equals the freshly
   computed canonical hash) → ``noop`` bucket. **This is the cheap
   path** — no backend call required.
5. **Mapping with different hash** → ``update`` bucket. If the adapter
   is available, we re-fetch live state and produce a per-field
   :class:`FieldDiff` list with ``risk_flag``-classification.

After per-Item processing, ``_detect_unmatched_orphans`` walks the
backend's product list under our target channels and records any
live product that has no mapping back to ERP — those become orphans.

The differ never writes to the DB. The orchestrator
(:mod:`tasks.apply_sync`) consumes the plan and persists state changes
(``Ecommerce Item.last_synced_hash``, ``Ecommerce Sync Run`` rows,
etc.) at apply time.
"""

from __future__ import annotations

import frappe
from frappe import _

from ecommerce_integrations.product_sync.constants import (
    ACTION_CREATE,
    ACTION_DEACTIVATE,
    ACTION_DELETE,
    ACTION_NOOP,
    ACTION_UPDATE,
    BACKEND_TO_INTEGRATION_KEY,
    OVERRIDE_DOCTYPE,
)
from ecommerce_integrations.product_sync.engine.adapters.base import (
    AdapterError,
    LiveProductNode,
)
from ecommerce_integrations.product_sync.engine.canonical import (
    build_canonical_payload,
    compute_hash,
)
from ecommerce_integrations.product_sync.engine.preview import (
    ConflictItem,
    FieldDiff,
    MappingDriftItem,
    OrphanProduct,
    ProductNodePlan,
    ProductSyncPreviewPlan,
)
from ecommerce_integrations.product_sync.resolver import resolve_item
from ecommerce_integrations.product_sync.walker import walk_items_for_sync

# Phase-5 default: no cap. Preview must give the operator accurate
# bucket counts over the FULL scope — otherwise the first-sync banner
# ("X creates, Y orphans") shows the artificial cap, not reality, and
# adopt-by-SKU sizes are wrong. For very large catalogs (>50k items)
# the caller should set ``max_items`` explicitly to avoid >60s freezes.
# No cap on the preview by default. Accurate bucket counts over the
# FULL scope are required — otherwise the first-sync banner and
# adopt-by-SKU sizing show artificial numbers. For catalogues much
# larger than ~50k items, callers should pass ``max_items`` explicitly
# to keep the synchronous freeze under a minute.
_DEFAULT_MAX_ITEMS_PREVIEW = None
_ECOMMERCE_ITEM_DOCTYPE = "Ecommerce Item"

# Risk thresholds for FieldDiff.risk_flag classification.
_PRICE_JUMP_PCT = 20.0
_STOCK_DROP_PCT = 50.0


def compute_product_diff(
    sync_doc,
    *,
    max_items: int | None = _DEFAULT_MAX_ITEMS_PREVIEW,
    fetch_live: bool = True,
    subset_item_codes: list[str] | None = None,
    on_progress=None,
    detect_orphans: bool = False,
) -> ProductSyncPreviewPlan:
    """Build a :class:`ProductSyncPreviewPlan` for one Sync.

    Args:
        sync_doc: The ``Ecommerce Product Sync`` doc.
        max_items: Cap on items processed for preview. ``None`` = no
            cap (the default — accurate counts are the point).
        fetch_live: When True, fetch live backend state for **drift
            candidates only** — items whose stored hash differs from
            the freshly computed proposed hash. Noop items (hash
            equal) never trigger a backend roundtrip, which is the
            single biggest preview speedup for stable catalogues.
            Pass False to skip the backend entirely (hash-only mode).
        subset_item_codes: Optional pre-filtered list of item_codes —
            the differ only processes these (used by the Subset-Test
            mode of the test runner).
        on_progress: Optional ``callable(current: int, total: int)`` —
            invoked while classifying items so a background runner can
            stream progress to the UI. The callback is the runner's
            responsibility to throttle (writes to a Run row are cheap
            in batches of ~100 ms but expensive every iteration).
        detect_orphans: When True, walks the target channels for
            backend products not in any mapping (the slow path that
            used to fire on every preview). Default False so previews
            are fast by default; the full report is opt-in.
    """
    backend = sync_doc.backend or ""
    integration_key = BACKEND_TO_INTEGRATION_KEY.get(backend, backend.lower())

    plan = ProductSyncPreviewPlan(
        sync=sync_doc.name,
        title=sync_doc.title or sync_doc.name,
        backend=backend,
    )

    # 1. Resolve the in-scope items via walker.
    item_nodes = list(walk_items_for_sync(sync_doc))
    plan.items_in_scope = len(item_nodes)

    if subset_item_codes:
        wanted = set(subset_item_codes)
        item_nodes = [n for n in item_nodes if n.item_code in wanted]
        plan.notes.append(_(
            "Subset-Modus: {0} von {1} Artikeln werden verarbeitet."
        ).format(len(item_nodes), plan.items_in_scope))
    elif max_items and len(item_nodes) > max_items:
        plan.notes.append(_(
            "Vorschau zeigt die ersten {0} von {1} Artikeln. "
            "Beim Apply werden alle verarbeitet."
        ).format(max_items, len(item_nodes)))
        item_nodes = item_nodes[:max_items]

    if not item_nodes:
        plan.notes.append(_("No items found in scope."))
        return plan

    # 2. Bulk-load mappings + per-item overrides + Item snapshots.
    # The BulkContext does the heavy DB lifting up-front (5 queries
    # instead of 4-per-item) so the per-item canonical-payload build
    # is purely in-memory work afterwards. At 30k items this is
    # ~200ms vs ~6 minutes for the per-item path.
    from ecommerce_integrations.product_sync.engine.bulk_context import BulkContext

    item_codes = [n.item_code for n in item_nodes]
    mapping_by_item, mapped_ext_ids = _load_mappings(item_codes, integration_key)
    override_by_item = _load_overrides(sync_doc.name, item_codes)
    price_lists = _collect_price_lists(sync_doc)
    ctx = BulkContext(item_codes, price_lists)

    # 3. Phase A — Hash-first classification.
    #
    # Walk every Item once and bucket it into CREATE / NOOP /
    # drift-candidate based on (mapping, stored_hash) alone. No
    # backend roundtrip yet. Hash equality is the cheap NOOP gate:
    # ~30k items become a single in-memory pass, no HTTPS at all.
    drift_candidates: list[tuple] = []  # (item, proposed, hash, name, mapping)
    total = len(item_nodes)
    for i, node in enumerate(item_nodes):
        override = override_by_item.get(node.item_code)
        if override and override.get("mode") == "skip":
            _bump_progress(on_progress, i + 1, total)
            continue

        decision = resolve_item(node.item_code, backend)
        if decision.sync_name and decision.sync_name != sync_doc.name:
            plan.conflicts.append(
                ConflictItem(
                    item_code=node.item_code,
                    competing_syncs=decision.competing_syncs or [],
                    resolution=decision.source or "manual_review",
                )
            )
            _bump_progress(on_progress, i + 1, total)
            continue

        item = ctx.item(node.item_code) if ctx is not None else None
        if item is None:
            try:
                item = frappe.get_doc("Item", node.item_code)
            except frappe.DoesNotExistError:
                _bump_progress(on_progress, i + 1, total)
                continue

        proposed = build_canonical_payload(item, sync_doc, ctx=ctx)
        proposed_hash = compute_hash(proposed)
        proposed_name = proposed.get("basic", {}).get("name") or item.item_name

        mapping = mapping_by_item.get(node.item_code)
        if mapping is None or not mapping.get("integration_item_code"):
            plan.creates.append(
                ProductNodePlan(
                    item_code=node.item_code,
                    sku=item.item_code,
                    proposed_name=proposed_name,
                    action=ACTION_CREATE,
                )
            )
            _bump_progress(on_progress, i + 1, total)
            continue

        external_id = mapping["integration_item_code"]
        stored_hash = mapping.get("last_synced_hash") or ""
        if stored_hash and stored_hash == proposed_hash:
            plan.noops.append(
                ProductNodePlan(
                    item_code=node.item_code,
                    sku=item.item_code,
                    proposed_name=proposed_name,
                    action=ACTION_NOOP,
                    current_external_id=external_id,
                )
            )
            _bump_progress(on_progress, i + 1, total)
            continue

        # Drift candidate — defer until we know whether to fetch live.
        drift_candidates.append(
            (item, proposed, proposed_hash, proposed_name, mapping),
        )
        _bump_progress(on_progress, i + 1, total)

    # 4. Phase B — Backend roundtrip for drift candidates only.
    live_by_ext_id: dict[str, LiveProductNode] = {}
    adapter_available = False
    adapter = None
    if fetch_live and (drift_candidates or detect_orphans):
        try:
            adapter = _get_adapter(backend)
            adapter_available = True
        except NotImplementedError:
            plan.notes.append(_(
                "Backend-Adapter ist noch nicht implementiert — "
                "Per-Feld-Diff und Orphan-Erkennung übersprungen."
            ))
        except Exception as exc:  # noqa: BLE001 — never crash the differ
            plan.notes.append(
                _("Unexpected error loading adapter: {0}").format(exc),
            )

    if adapter_available and adapter is not None and drift_candidates:
        import time as _t
        drift_ext_ids = [
            m["integration_item_code"]
            for (_i, _p, _h, _n, m) in drift_candidates
        ]
        fetch_total = len(drift_ext_ids)
        fetched = 0
        last_keepalive = _t.time()
        try:
            for p in adapter.fetch_products(external_ids=drift_ext_ids):
                live_by_ext_id[p.external_id] = p
                fetched += 1
                # Two reasons to tick periodically during the backend
                # fetch: (a) drive the worker's progress writer so the
                # bar moves past 99% instead of looking frozen,
                # (b) keep the MySQL connection warm. A long IO-bound
                # fetch with no DB activity blows past ``wait_timeout``
                # and the post-fetch ``set_value`` dies with (2006).
                now = _t.time()
                if now - last_keepalive > 2.0:
                    last_keepalive = now
                    _bump_progress(
                        on_progress,
                        total + fetched, total + fetch_total,
                    )
                    try:
                        frappe.db.sql("SELECT 1")
                    except Exception:  # noqa: BLE001
                        pass
        except AdapterError as exc:
            plan.notes.append(
                _("Backend-Fetch (Drift-Kandidaten) fehlgeschlagen: {0}").format(exc),
            )

    # 5. Phase C — Resolve drift candidates → UPDATE or MAPPING_DRIFT.
    for (item, proposed, proposed_hash, proposed_name, mapping) in drift_candidates:
        external_id = mapping["integration_item_code"]
        if adapter_available and external_id not in live_by_ext_id:
            plan.mapping_drift.append(
                MappingDriftItem(
                    item_code=item.item_code,
                    stale_external_id=external_id,
                    proposed_name=proposed_name,
                )
            )
            continue
        live = live_by_ext_id.get(external_id) if adapter_available else None
        diffs = _diff_proposed_against_live(proposed, live, mapping=mapping) if live else []
        _apply_risk_flags(diffs)
        plan.updates.append(
            ProductNodePlan(
                item_code=item.item_code,
                sku=item.item_code,
                proposed_name=proposed_name,
                action=ACTION_UPDATE,
                diffs=diffs,
                current_external_id=external_id,
                notes=(
                    []
                    if live is not None
                    else [_(
                        "Per-Feld-Diff nicht verfügbar — Backend-Adapter konnte "
                        "Live-Stand nicht laden."
                    )]
                ),
            )
        )

    # 6. Phase D — Orphan walk, opt-in only.
    #
    # Used to fire on every preview, but on a 30k+ catalogue the
    # ``adapter.fetch_products(sales_channel_ids=...)`` call traverses
    # the entire backend inventory and dominated wall-time (the famous
    # "stuck at 99 %"). Now strictly opt-in via ``detect_orphans``.
    if detect_orphans and adapter_available and adapter is not None:
        _detect_unmatched_orphans(plan, sync_doc, adapter, mapped_ext_ids)
    elif not detect_orphans:
        plan.notes.append(_(
            "Orphan-Erkennung übersprungen (Schnellvorschau). "
            "Für die vollständige Liste 'Detailvergleich' wählen."
        ))

    return plan


def _bump_progress(on_progress, current: int, total: int) -> None:
    """Throttled progress shim — never raises, never fails the diff."""
    if on_progress is None:
        return
    try:
        on_progress(current, total)
    except Exception:  # noqa: BLE001
        pass


def _diff_proposed_against_live(
    proposed: dict, live: LiveProductNode, *, mapping: dict | None = None,
) -> list[FieldDiff]:
    """Field-by-field comparison: proposed canonical vs live snapshot.

    Compares the structured sections of the canonical payload to the
    flat ``LiveProductNode`` dataclass. Only emits FieldDiffs where
    values actually differ.

    ``mapping`` is the ``tabEcommerce Item`` row for this Item (or
    None on first sync). The image diff reads ``mapping.
    pushed_image_map`` to short-circuit drift on basenames we've
    already uploaded.
    """
    diffs: list[FieldDiff] = []

    # Each section diff is gated on whether the corresponding canonical
    # section is present. The canonical only emits a section when the
    # matching ``sync_*_fields`` toggle is on, so a missing section
    # means "we wouldn't push this anyway — don't surface it as drift".
    # Without this gate every disabled section would still flag every
    # item, misleading the operator about what apply would actually do.

    basic = proposed.get("basic") or {}
    if basic:
        _maybe_diff(diffs, "basic.name", live.name or "", basic.get("name") or "")
        _maybe_diff(diffs, "basic.sku", live.sku or "", basic.get("sku") or "")
        _maybe_diff_long(
            diffs,
            "basic.description",
            live.description or "",
            basic.get("description") or "",
        )
        _maybe_diff(
            diffs,
            "basic.is_active",
            str(bool(live.active)),
            str(bool(basic.get("is_active"))),
        )

    pricing = proposed.get("pricing") or {}
    if pricing:
        prop_price = pricing.get("base_price")
        live_price = live.price
        if prop_price is not None and live_price is not None:
            if abs(float(prop_price) - float(live_price)) > 0.001:
                diffs.append(
                    FieldDiff(
                        field="pricing.base_price",
                        current=str(live_price),
                        proposed=str(prop_price),
                        change_kind="modified",
                    )
                )

    inventory = proposed.get("inventory") or {}
    if inventory:
        prop_qty = inventory.get("qty")
        live_qty = live.stock
        if (
            prop_qty is not None
            and live_qty is not None
            and float(prop_qty) != float(live_qty)
        ):
            diffs.append(
                FieldDiff(
                    field="inventory.qty",
                    current=str(live_qty),
                    proposed=str(prop_qty),
                    change_kind="modified",
                )
            )

    # Image diff: three-stage comparison so we don't flag spurious
    # drift on every preview.
    #
    # 1. Persisted ``pushed_image_map`` (ERP basename → Shopware
    #    media UUID): if every proposed ERP image has a mapping AND
    #    the live product reports those URLs, no diff.
    # 2. Filename-basename comparison: ERPNext URLs (``/file/hash/
    #    name.jpg``) and Shopware CDN URLs (``cdn.x/.../name.jpg``)
    #    keep the same basename after a round-trip.
    # 3. Count fallback when basenames don't anchor reliably.
    def _basename(u: str) -> str:
        tail = (u or "").rstrip("/").rsplit("/", 1)[-1]
        tail = tail.split("?", 1)[0]
        return tail.lower()

    prop_images = [img["url"] for img in proposed.get("images", [])]
    live_images = list(live.images or [])
    prop_names = {_basename(u) for u in prop_images if u}
    live_names = {_basename(u) for u in live_images if u}

    # Stage 1 — persisted map. The orchestrator wrote a dict of
    # ``{erp_basename: shopware_media_uuid}`` after the last
    # successful image upload. If every current proposed basename
    # has a map entry AND the live image set contains the same
    # basenames, suppress the diff. The differ doesn't receive the
    # map directly — it's read off ``Ecommerce Item`` via the
    # mapping dict in the caller's scope (``mapping`` argument).
    pushed_map_json = (mapping or {}).get("pushed_image_map") or ""
    if pushed_map_json and prop_names:
        try:
            import json as _json
            pushed_map = _json.loads(pushed_map_json)
            mapped_basenames = {k.lower() for k in pushed_map.keys()}
            # Are ALL proposed images already pushed AND do they
            # all appear in the live image set?
            if prop_names.issubset(mapped_basenames) and prop_names.issubset(live_names):
                # No diff — short-circuit.
                pass
            elif prop_names != live_names:
                # Fall through to the normal diff path below.
                pushed_map = None
            else:
                pushed_map = "match"
        except (ValueError, TypeError):
            pushed_map = None
        if pushed_map == "match" or (
            pushed_map and prop_names.issubset(mapped_basenames)
            and prop_names.issubset(live_names)
        ):
            # Skip image diff — already correctly pushed.
            return diffs

    if prop_names != live_names:
        added = sorted(prop_names - live_names)
        removed = sorted(live_names - prop_names)
        diffs.append(
            FieldDiff(
                field="images",
                current=f"{len(live_images)} Bilder",
                proposed=f"{len(prop_images)} Bilder",
                change_kind="modified",
                preview_current=", ".join(list(live_names)[:3]),
                preview_proposed=f"+{len(added)} hinzu, −{len(removed)} entf.",
            )
        )

    # Categories: compare the resolved UUIDs that canonical emits to the
    # live UUIDs from Shopware. Only emit a diff when the operator has
    # actually mapped the IG (``ids`` non-empty) and the set differs.
    # When mapping is missing the apply step does NOT push the
    # ``categories`` field — emitting a "would change" diff there
    # would lie to the operator.
    cat_section = proposed.get("categories") or {}
    prop_cat_ids = sorted(set(cat_section.get("ids") or []))
    live_cat_ids = sorted(set(live.category_ids or []))
    if prop_cat_ids and prop_cat_ids != live_cat_ids:
        diffs.append(
            FieldDiff(
                field="categories",
                current=", ".join(live_cat_ids)[:200] or None,
                proposed=", ".join(prop_cat_ids)[:200] or None,
                change_kind="modified",
            )
        )

    # Properties: brand + manufacturer + attribute set. Gated on the
    # presence of the proposed section (which itself is gated on
    # ``sync_properties``).
    proposed_props = proposed.get("properties")
    live_props = (live.properties or {}) if proposed_props is not None else {}
    proposed_props = proposed_props or {}
    for key in ("brand", "manufacturer") if proposed.get("properties") is not None else ():
        _maybe_diff(
            diffs,
            f"properties.{key}",
            live_props.get(key) or "",
            proposed_props.get(key) or "",
        )
    prop_attrs = {
        (a.get("name") or "", a.get("value") or "")
        for a in (proposed_props.get("attributes") or [])
        if isinstance(a, dict)
    }
    live_attrs = {
        (a.get("name") or "", a.get("value") or "")
        for a in (live_props.get("attributes") or [])
        if isinstance(a, dict)
    }
    if proposed.get("properties") is not None and prop_attrs != live_attrs:
        added = sorted(prop_attrs - live_attrs)
        removed = sorted(live_attrs - prop_attrs)
        diffs.append(
            FieldDiff(
                field="properties.attributes",
                current=f"{len(live_attrs)} Attribute",
                proposed=f"{len(prop_attrs)} Attribute",
                change_kind="modified",
                preview_current=", ".join(f"{n}={v}" for n, v in list(live_attrs)[:3])[:200],
                preview_proposed=f"+{len(added)} hinzu, −{len(removed)} entf.",
            )
        )

    # SEO: slug, meta_title, meta_description. Gated on the canonical
    # actually emitting a ``seo`` section (i.e. ``sync_seo_fields=1``)
    # — otherwise the apply wouldn't push SEO and surfacing a diff
    # would mislead the operator.
    if proposed.get("seo") is not None:
        proposed_seo = proposed.get("seo") or {}
        live_seo = live.seo or {}
        for key in ("slug", "meta_title", "meta_description"):
            _maybe_diff_long(
                diffs,
                f"seo.{key}",
                live_seo.get(key) or "",
                proposed_seo.get(key) or "",
            )

    return diffs


def _maybe_diff(diffs: list[FieldDiff], field: str, cur, prop) -> None:
    if cur == prop:
        return
    diffs.append(
        FieldDiff(
            field=field,
            current=None if cur in ("", None) else str(cur),
            proposed=None if prop in ("", None) else str(prop),
            change_kind=_classify(cur, prop),
        )
    )


def _maybe_diff_long(diffs: list[FieldDiff], field: str, cur: str, prop: str) -> None:
    if cur == prop:
        return
    diffs.append(
        FieldDiff(
            field=field,
            current=cur,
            proposed=prop,
            change_kind=_classify(cur, prop),
            preview_current=(cur or "")[:200],
            preview_proposed=(prop or "")[:200],
        )
    )


def _classify(cur, prop) -> str:
    if cur in (None, "", [], {}) and prop not in (None, "", [], {}):
        return "added"
    if cur not in (None, "", [], {}) and prop in (None, "", [], {}):
        return "removed"
    return "modified"


def _apply_risk_flags(diffs: list[FieldDiff]) -> None:
    """Tag FieldDiffs whose value movement hits a risk threshold.

    Banner counters in the preview UI read ``risk_flag`` directly so
    this is the single source of truth for "is this push suspicious".
    """
    for d in diffs:
        if d.field == "pricing.base_price":
            try:
                cur = float(d.current) if d.current else 0.0
                prop = float(d.proposed) if d.proposed else 0.0
                if cur > 0:
                    pct = abs(prop - cur) / cur * 100.0
                    if pct > _PRICE_JUMP_PCT:
                        d.risk_flag = "price_jump"
            except (ValueError, TypeError):
                continue
        elif d.field == "inventory.qty":
            try:
                cur = float(d.current) if d.current else 0.0
                prop = float(d.proposed) if d.proposed else 0.0
                if cur > 0 and prop < cur:
                    pct = (cur - prop) / cur * 100.0
                    if pct > _STOCK_DROP_PCT:
                        d.risk_flag = "stock_drop"
            except (ValueError, TypeError):
                continue
        elif d.field == "basic.name":
            if d.current and d.proposed:
                cur_words = set(d.current.lower().split())
                prop_words = set(d.proposed.lower().split())
                if cur_words and prop_words and not (cur_words & prop_words):
                    d.risk_flag = "name_rewrite"


def _load_mappings(
    item_codes: list[str], integration_key: str,
) -> tuple[dict[str, dict], list[str]]:
    """Bulk-load ``tabEcommerce Item`` rows for the given item_codes.

    Returns ``(mapping_by_item_code, mapped_external_ids)``.
    """
    if not item_codes:
        return {}, []
    rows = frappe.get_all(
        _ECOMMERCE_ITEM_DOCTYPE,
        filters={
            "erpnext_item_code": ("in", item_codes),
            "integration": integration_key,
        },
        fields=[
            "name",
            "erpnext_item_code",
            "integration_item_code",
            "last_synced_hash",
            "last_synced_at",
            # JSON map of ERP basename → Shopware media UUID written
            # by the apply step. Differ uses it to short-circuit
            # image-drift on items where every image has been
            # uploaded already.
            "pushed_image_map",
        ],
        limit=0,
    )
    mapping_by_item = {r["erpnext_item_code"]: r for r in rows}
    mapped_ext_ids = [
        r["integration_item_code"] for r in rows if r.get("integration_item_code")
    ]
    return mapping_by_item, mapped_ext_ids


def _collect_price_lists(sync_doc) -> list[str]:
    """Every Price List the canonical-pricing builder might query —
    pre-loaded once via :class:`BulkContext` so the per-item loop
    stays in-memory."""
    lists: set[str] = set()
    global_list = (getattr(sync_doc, "price_list_override", None) or "").strip()
    if global_list:
        lists.add(global_list)
    for row in (getattr(sync_doc, "target_sales_channels", []) or []):
        override = (getattr(row, "override_price_list", None) or "").strip()
        if override:
            lists.add(override)
    return sorted(lists)


def _load_overrides(sync_name: str, item_codes: list[str]) -> dict[str, dict]:
    """Bulk-load child-table overrides for the active items."""
    if not item_codes:
        return {}
    rows = frappe.get_all(
        OVERRIDE_DOCTYPE,
        filters={
            "parent": sync_name,
            "parenttype": "Ecommerce Product Sync",
            "item_code": ("in", item_codes),
        },
        fields=[
            "item_code",
            "mode",
            "pinned_external_id",
            "custom_name",
            "custom_price",
            "custom_description",
            "force_visibility_only_on_channels",
        ],
        limit=0,
    )
    return {r["item_code"]: r for r in rows}


def _detect_unmatched_orphans(
    plan: ProductSyncPreviewPlan,
    sync_doc,
    adapter,
    mapped_ext_ids: list[str],
) -> None:
    """Walk all live products in the target channels — anything not
    in our mapping table is an orphan candidate."""
    sales_channels = [
        c.sales_channel_id
        for c in (sync_doc.target_sales_channels or [])
        if c.sales_channel_id
    ]
    if not sales_channels:
        return
    mapped_set = set(mapped_ext_ids)
    try:
        for live in adapter.fetch_products(sales_channel_ids=sales_channels):
            if live.external_id in mapped_set:
                continue
            plan.orphans.append(
                OrphanProduct(
                    external_id=live.external_id,
                    name=live.name or "",
                    sku=live.sku,
                    suggested_action="keep",
                )
            )
    except NotImplementedError:
        # Adapter walks aren't implemented yet — drop silently, the
        # mapped-set diff already gave us drift detection above.
        return
    except AdapterError as exc:
        plan.notes.append(_("Orphan detection skipped: {0}").format(exc))
        return


def _get_adapter(backend: str):
    """Lazy adapter import to keep the differ module standalone."""
    from ecommerce_integrations.product_sync.engine.registry import get_adapter
    return get_adapter(backend)
