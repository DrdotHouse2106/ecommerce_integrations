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
) -> ProductSyncPreviewPlan:
    """Build a :class:`ProductSyncPreviewPlan` for one Sync.

    Args:
        sync_doc: The ``Ecommerce Product Sync`` doc.
        max_items: Cap on items processed for preview. ``None`` = no
            cap (the default — accurate counts are the point).
        fetch_live: When True, calls the backend adapter to fetch live
            products for per-field diffs and orphan/drift detection.
            Pass False for hash-only fast mode when the adapter is
            slow or unavailable.
        subset_item_codes: Optional pre-filtered list of item_codes —
            the differ only processes these (used by the Subset-Test
            mode of the test runner).
        on_progress: Optional ``callable(current: int, total: int)`` —
            invoked while classifying items so a background runner can
            stream progress to the UI. The callback is the runner's
            responsibility to throttle (writes to a Run row are cheap
            in batches of ~100 ms but expensive every iteration).
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
        plan.notes.append(_("Keine Artikel in der Auswahl gefunden."))
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

    # 3. Optionally pre-fetch live products for diff + orphan/drift.
    live_by_ext_id: dict[str, LiveProductNode] = {}
    adapter_available = False
    adapter = None
    if fetch_live:
        try:
            adapter = _get_adapter(backend)
            if mapped_ext_ids:
                # Fetch the mapped products first so the differ has live
                # state for every update candidate.
                for p in adapter.fetch_products(external_ids=mapped_ext_ids):
                    live_by_ext_id[p.external_id] = p
            adapter_available = True
        except NotImplementedError:
            plan.notes.append(_(
                "Backend-Adapter ist noch nicht implementiert — "
                "Per-Feld-Diff und Orphan-Erkennung übersprungen."
            ))
        except AdapterError as exc:
            plan.notes.append(_("Backend-Fetch fehlgeschlagen: {0}").format(exc))
        except Exception as exc:  # noqa: BLE001 — never crash the differ
            plan.notes.append(
                _("Unerwarteter Fehler beim Backend-Fetch: {0}").format(exc),
            )

    # 4. Walk each item, fill the appropriate bucket.
    total = len(item_nodes)
    for i, node in enumerate(item_nodes):
        _classify_item(
            plan=plan,
            node=node,
            sync_doc=sync_doc,
            backend=backend,
            mapping=mapping_by_item.get(node.item_code),
            override=override_by_item.get(node.item_code),
            live_by_ext_id=live_by_ext_id,
            adapter_available=adapter_available,
            ctx=ctx,
        )
        if on_progress is not None:
            try:
                on_progress(i + 1, total)
            except Exception:  # noqa: BLE001
                pass  # progress is best-effort, never fail the diff

    # 5. Unmatched orphans: live products in the target channels not in
    # any mapping row.
    if adapter_available and adapter is not None:
        _detect_unmatched_orphans(plan, sync_doc, adapter, mapped_ext_ids)

    return plan


def _classify_item(
    *,
    plan: ProductSyncPreviewPlan,
    node,
    sync_doc,
    backend: str,
    mapping: dict | None,
    override: dict | None,
    live_by_ext_id: dict[str, LiveProductNode],
    adapter_available: bool,
    ctx=None,
) -> None:
    """Place one Item into the correct bucket of ``plan``."""
    # Override mode=skip wins immediately, no further work.
    if override and override.get("mode") == "skip":
        return

    # Conflict resolution.
    decision = resolve_item(node.item_code, backend)
    if decision.sync_name and decision.sync_name != sync_doc.name:
        plan.conflicts.append(
            ConflictItem(
                item_code=node.item_code,
                competing_syncs=decision.competing_syncs or [],
                resolution=decision.source or "manual_review",
            )
        )
        return

    # Use the bulk-pre-loaded ItemSnapshot when available; fall back
    # to a per-item get_doc only if the snapshot is missing (rare —
    # walker raced with a delete, or a single-item caller skipped the
    # bulk context).
    item = ctx.item(node.item_code) if ctx is not None else None
    if item is None:
        try:
            item = frappe.get_doc("Item", node.item_code)
        except frappe.DoesNotExistError:
            return

    proposed = build_canonical_payload(item, sync_doc, ctx=ctx)
    proposed_hash = compute_hash(proposed)
    proposed_name = proposed.get("basic", {}).get("name") or item.item_name

    # No mapping yet → CREATE.
    if mapping is None or not mapping.get("integration_item_code"):
        plan.creates.append(
            ProductNodePlan(
                item_code=node.item_code,
                sku=item.item_code,
                proposed_name=proposed_name,
                action=ACTION_CREATE,
            )
        )
        return

    external_id = mapping["integration_item_code"]
    stored_hash = mapping.get("last_synced_hash") or ""

    # Mapping points at a live product that no longer exists → DRIFT.
    if adapter_available and external_id not in live_by_ext_id:
        plan.mapping_drift.append(
            MappingDriftItem(
                item_code=node.item_code,
                stale_external_id=external_id,
                proposed_name=proposed_name,
            )
        )
        return

    # Hash unchanged → NOOP (cheap path, no field-by-field work).
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
        return

    # Hash drift → UPDATE.
    live = live_by_ext_id.get(external_id) if adapter_available else None
    diffs = _diff_proposed_against_live(proposed, live) if live else []
    _apply_risk_flags(diffs)
    plan.updates.append(
        ProductNodePlan(
            item_code=node.item_code,
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


def _diff_proposed_against_live(
    proposed: dict, live: LiveProductNode,
) -> list[FieldDiff]:
    """Field-by-field comparison: proposed canonical vs live snapshot.

    Compares the structured sections of the canonical payload to the
    flat ``LiveProductNode`` dataclass. Only emits FieldDiffs where
    values actually differ.
    """
    diffs: list[FieldDiff] = []

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

    prop_images = [img["url"] for img in proposed.get("images", [])]
    live_images = list(live.images or [])
    if set(prop_images) != set(live_images):
        added = sorted(set(prop_images) - set(live_images))
        removed = sorted(set(live_images) - set(prop_images))
        diffs.append(
            FieldDiff(
                field="images",
                current=f"{len(live_images)} Bilder",
                proposed=f"{len(prop_images)} Bilder",
                change_kind="modified",
                preview_current=", ".join(live_images[:3]),
                preview_proposed=f"+{len(added)} hinzu, −{len(removed)} entf.",
            )
        )

    # Categories: compare live.category_ids (raw) to mapping resolved at
    # apply time; Phase-2 emits a coarse hint.
    prop_categories = (proposed.get("categories") or {}).get("item_group", "")
    if prop_categories and prop_categories not in (live.category_ids or []):
        diffs.append(
            FieldDiff(
                field="categories.item_group",
                current=", ".join(live.category_ids or [])[:200] or None,
                proposed=prop_categories,
                change_kind="modified",
            )
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
        plan.notes.append(_("Orphan-Erkennung übersprungen: {0}").format(exc))
        return


def _get_adapter(backend: str):
    """Lazy adapter import to keep the differ module standalone."""
    from ecommerce_integrations.product_sync.engine.registry import get_adapter
    return get_adapter(backend)
