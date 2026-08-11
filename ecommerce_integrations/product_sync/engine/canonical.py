"""Canonical payload + hash for delta-detection.

The Phase-3 differ asks two questions per Item: (1) what would I push?
and (2) is that different from what I last pushed? This module is the
answer to both.

``build_canonical_payload(item, sync)`` walks the Item, reads the
Sync doc's field toggles, and emits a deterministic, JSON-serialisable
dict. ``compute_hash(payload)`` hashes that dict with SHA-256.

Determinism rules — break any of these and hashes spuriously change:

1. **Stable key order** — JSON is serialised with ``sort_keys=True``.
2. **Stable string form** — strings are stripped + internal whitespace
   collapsed to single spaces; ``None`` becomes ``""``; missing
   attributes are treated as ``None``.
3. **Stable numeric form** — floats are rounded to 4 decimal places
   (cents-precise for typical retail prices). ``Decimal`` is funnelled
   through the same rounding.
4. **Stable list order** — lists of structured items (images,
   attributes, channel prices) are sorted on a stable key (URL, name,
   channel_id).
5. **Schema version tag** — the top-level ``v`` field bumps when we
   add a section in a backward-incompatible way, so old hashes never
   collide with new ones.
6. **Toggle-aware** — sections only land in the payload when the
   matching ``sync_*`` toggle is set, so an operator who switches
   ``sync_images=0`` does not trigger every item to "drift".
7. **Backend-agnostic** — the same payload shape is used for Shopware
   and Medusa. Adapters compute the same hash from their live data so
   the differ can compare apples to apples.

The module is intentionally thin on ``frappe.*``-imports: most
callsites pass in a pre-loaded ``item`` doc (from ``frappe.get_doc``)
plus a ``sync`` doc. Imports happen lazily inside the section helpers
where Bin/Item-Price/File lookups need them.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

# Schema version. Bump only on backward-incompatible changes to the
# payload shape (added/removed top-level keys, changed semantics). Do
# NOT bump for "we now collect a new property" — that is data drift,
# not schema drift, and will naturally produce different hashes.
#
# v3 (2026-06): ``properties.ecommerce_properties`` rows now carry
# ``group_order`` + ``option_order`` from the
# ``Ecommerce Property Group`` catalog, and rows are sorted by those
# instead of ``(name, value)``. The catalog gives the operator one
# global knob per property to retune storefront ordering (Shopware
# only supports per-group position anyway); the position landing in
# the hash means re-ordering the catalog naturally triggers a re-push
# to Shopware and Medusa. The bump invalidates cached hashes so every
# item re-emits canonical with the new shape on the next sync.
#
# v4 (2026-06): ``pricing.base_net`` added so the Medusa adapter can
# send net prices (Medusa's pricing module derives gross via the
# region's tax_rate at display time, producing consistent
# tax-on-sum rounding for multi-line orders — Shopware keeps using
# the existing ``base_price`` gross value).
#
# v5 (2026-06): ``basic.min_order_qty`` added (sparse — only emitted
# when > 1) so carton-based minimum order quantities reach Shopware's
# ``minPurchase`` / ``purchaseSteps``. Sparse emission keeps the hash
# of the overwhelming majority of items (no MOQ) stable across the
# version bump; removing an Item's MOQ drops the key, flips the
# ``basic`` hash and the adapter pushes the reset (minPurchase=1).
#
# v6 (2026-06): ``properties.custom_fields`` added (sparse — only when
# the item has Custom-Field-typed ecommerce_properties flagged for this
# backend). Custom-Field rows are machine/integration markers, kept out
# of the customer-facing ``ecommerce_properties`` display list. The
# Medusa adapter folds them into top-level product ``metadata`` so a
# row marked ``sync_to_medusa`` but typed Custom Field actually reaches
# Medusa (previously it reached no channel — Shopware customFields gate
# on ``sync_to_shopware``, the Medusa metadata.properties list excludes
# Custom-Field rows).
#
# v7 (2026-07): ``basic.restock_time`` added (sparse — only when > 0)
# so Shopware's "Nachbestellung in X Tagen" (restockTime) reaches the
# product. ``basic.delivery_time`` now also folds in Shopware Setting's
# ``default_delivery_time`` fallback (and restock_time folds in
# ``default_restock_time``) when the Item's own field is empty — the
# bump ensures a change to either global default re-hashes every item
# still relying on the fallback, instead of silently going stale.
#
# v8 (2026-08): ``basic.weight``/``height``/``width``/``length`` and
# ``basic.is_closeout`` added. These previously only existed in the
# dead legacy ``export/product_mapper.py`` uploader — the live
# per-Item-save engine never pushed them, so shipping-cost calculation
# (weight/dimension-based carrier rules) and closeout badge/stock=0
# handling silently stopped working once the engine replaced the
# legacy uploader as the active push path. Sparse emission (only when
# set/nonzero) so items without WeClapp's ``item_height``/``width``/
# ``length``/``abverkauf`` custom fields keep a stable hash.
#
# v9 (2026-08): ``basic.grundpreis_unit``/``grundpreis_reference_qty``/
# ``grundpreis_purchase_qty`` added (sparse — only when WeClapp's
# ``grundpreis_masseinheit`` is set) so Shopware's price-per-unit
# ("Grundpreis", PAngV) display reaches the storefront for the items
# that legally require it. Gated on the *unit* field specifically
# (rather than the two decimal fields) because only a minority of
# items need a Grundpreis at all — most items legitimately have all
# three fields empty, and that emptiness must stay hash-stable.
PAYLOAD_VERSION = 9

# Float precision for hashing. 4 decimals is "1/100th of a cent" —
# plenty for retail prices and stock floats; small enough to swallow
# float-arithmetic noise.
_FLOAT_DECIMALS = 4


def build_canonical_payload(item, sync, ctx=None) -> dict[str, Any]:
    """Return the canonical payload dict for one Item under one Sync.

    ``item`` can be either a real Frappe Item doc (with all child
    tables resolved) or an ``ItemSnapshot`` from
    :class:`BulkContext` — both quack the same way for the attributes
    we read. ``ctx`` is the optional :class:`BulkContext`; when set,
    expensive lookups (stock, price, images) bypass per-item queries
    and hit the pre-loaded in-memory dicts.
    """
    payload: dict[str, Any] = {
        "v": PAYLOAD_VERSION,
        "item_code": _norm_str(item.item_code),
        "is_variant": bool(getattr(item, "variant_of", None)),
        # ``variant_of`` is NOT hashed: ERPNext stores the parent's
        # item_code, Shopware stores its own parentId UUID — they can
        # never match, so including either side would force every
        # variant Item to look like permanent drift. The is_variant
        # boolean is enough for hash parity.
    }
    if _flag(sync, "sync_basic_fields", default=True):
        payload["basic"] = _canonical_basic(item, sync)
        # Dynamic Item→Shopware customField mappings configured by
        # the operator on Shopware Setting.item_custom_field_mappings.
        # Lives under ``basic`` so it shares the section's delta gate
        # — a mapping value change flips the basic hash and the
        # payload builder re-emits ``customFields``.
        dyn = _canonical_dynamic_custom_fields(item)
        if dyn:
            payload["basic"]["dynamic_custom_fields"] = dyn
        # ``customFields`` is replicated per-language onto the wire (see
        # ``build_shopware_payload``). Fold the resolved target language IDs
        # into the basic hash so a change to the ``custom_field_sync_languages``
        # setting — or a newly added Shopware language — flips the hash and the
        # item re-pushes its per-language customFields through the normal delta
        # gate (no manual force, no PAYLOAD_VERSION bump). Hash-only: the
        # payload builder never reads this key, so it stays off the wire. Only
        # added when the item actually emits customFields, so items without any
        # stay hash-stable.
        _b = payload["basic"]
        if (
            _b.get("dynamic_custom_fields")
            or _b.get("ai_short_description")
            or _b.get("ai_benefits")
            or _b.get("ai_seo_description")
        ):
            from ecommerce_integrations.product_sync.engine.payload import (
                _custom_field_sync_setting,
            )

            # Hash the durable *setting* (DB-only), not the live-resolved
            # language IDs — so a Shopware API hiccup during canonical build
            # can't flip the hash and cause spurious re-pushes. A setting change
            # still flips it; adding a brand-new Shopware language while the
            # setting stays ``all`` needs a one-off forced re-sync (documented).
            _b["custom_field_languages"] = _custom_field_sync_setting()
    # Per-item Sales-Channel visibility, resolved via the shared
    # Catalog-Mirror resolver (Smart Collections + Catalog Mirror
    # placements + per-item channel overrides). Owns its own
    # canonical section so a Smart Collection membership change
    # flips the hash → drift → re-push the visibility set without
    # touching the unrelated basic / pricing / properties sections.
    #
    # Opt-in per Sync via the ``sync_visibilities`` flag, default
    # OFF. Operators need to first ensure Catalog Mirror +
    # Smart Collections cover every (item, channel) placement they
    # want preserved — without that pre-check, flipping the flag
    # would prune live visibility entries the resolver doesn't
    # know about (typically items left behind from a legacy
    # uploader run on a channel that no Catalog-Mirror / Smart
    # Collection currently targets). Both backends supported:
    # Shopware projects onto ``product.visibilities`` (with the
    # 10/20/30 visibility-level enum), Medusa onto
    # ``product.sales_channels`` (binary in/out, all entries get
    # visibility=30 by convention to keep the canonical shape
    # symmetric).
    if _flag(sync, "sync_visibilities", default=False):
        payload["visibilities"] = _canonical_visibilities(item, sync)
    if _flag(sync, "sync_pricing", default=True):
        payload["pricing"] = _canonical_pricing(item, sync, ctx)
    if _flag(sync, "sync_inventory", default=True):
        payload["inventory"] = _canonical_inventory(item, sync, ctx)
    if _flag(sync, "sync_images", default=True):
        payload["images"] = _canonical_images(item, sync, ctx)
    if _flag(sync, "sync_properties", default=True):
        payload["properties"] = _canonical_properties(item, sync)
    if _flag(sync, "sync_seo_fields", default=False):
        payload["seo"] = _canonical_seo(item, sync)
    if _flag(sync, "sync_taxes", default=True):
        payload["taxes"] = _canonical_taxes(item, sync)
    # Category assignment isn't a sync_* toggle — products in
    # Shopware/Medusa MUST belong to at least one category, so the
    # mapping is always part of the payload. Drift here triggers a
    # re-assignment push.
    payload["categories"] = _canonical_categories(item, sync)
    return payload


def compute_hash(payload: dict[str, Any]) -> str:
    """SHA-256 hex digest of the canonical JSON serialisation."""
    return hashlib.sha256(_serialise(payload).encode("utf-8")).hexdigest()


def compute_hash_for(item, sync) -> str:
    """Build canonical payload and hash it in one go."""
    return compute_hash(build_canonical_payload(item, sync))


# ─── Per-field delta — canonical (de)serialisation ─────────────────────


def encode_canonical(payload: dict[str, Any]) -> str:
    """gzip-then-base64 the canonical so it fits a single
    ``Ecommerce Item.last_synced_canonical`` (Long Text) column.

    Typical compression on the canonical's repetitive JSON shape is
    ~10×, so a 5–10 kB raw payload lands at ~500–1000 bytes stored.
    """
    import base64
    import gzip
    raw = _serialise(payload).encode("utf-8")
    return base64.b64encode(gzip.compress(raw, mtime=0)).decode("ascii")


def decode_canonical(stored: str | None) -> dict[str, Any] | None:
    """Reverse of :func:`encode_canonical`. Returns ``None`` on missing
    or corrupt input — callers treat that as "no prior canonical
    stored, push everything"."""
    if not stored:
        return None
    import base64
    import gzip
    try:
        return json.loads(gzip.decompress(base64.b64decode(stored)).decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None


# Top-level canonical sections that the apply path treats as
# independent change-units. A section listed here corresponds to one
# block of ``build_*_payload`` output AND/OR one Phase-C enrichment
# call — the apply path uses set membership to decide what to push.
CANONICAL_SECTIONS: tuple[str, ...] = (
    "basic",
    "pricing",
    "inventory",
    "images",
    "properties",
    "seo",
    "taxes",
    "categories",
    # Per-item Sales-Channel visibility (opt-in via ``sync_visibilities``).
    # Listed here so a visibility-only change produces a non-empty
    # ``changed_sections`` set and the apply pipeline's Phase-C
    # visibility reconcile can gate on it.
    "visibilities",
)


def changed_sections(
    stored: dict[str, Any] | None,
    proposed: dict[str, Any],
) -> set[str]:
    """Return the canonical section names whose serialised form
    differs between ``stored`` and ``proposed``.

    Sections present only in ``proposed`` (e.g. a new field gated by a
    just-flipped sync toggle) count as changed. Sections present only
    in ``stored`` but removed from ``proposed`` ALSO count as changed
    — the apply path may want to push a clearing value.

    When ``stored`` is ``None`` every section in ``proposed`` is
    returned: first-time pushes have no prior canonical to compare
    against and need the full payload.
    """
    if stored is None:
        return {sec for sec in CANONICAL_SECTIONS if sec in proposed}
    changed: set[str] = set()
    for sec in CANONICAL_SECTIONS:
        cur = stored.get(sec)
        prop = proposed.get(sec)
        # Compare the deterministically-serialised form so dict-key
        # order or list-internal ordering quirks don't leak through.
        if _serialise_value(cur) != _serialise_value(prop):
            changed.add(sec)
    return changed


def _serialise_value(value: Any) -> str:
    """Stable string repr of a canonical sub-value — same rules as the
    top-level :func:`_serialise` so nested comparisons match the hash
    semantics exactly."""
    if value is None:
        return "null"
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=_default,
    )


def diff_payloads(
    current: dict[str, Any] | None, proposed: dict[str, Any],
) -> list[dict[str, Any]]:
    """Per-field diff between two canonical payloads.

    Walks the proposed payload top-down. For each leaf field returns a
    ``{field, current, proposed, change_kind}`` dict matching the
    ``FieldDiff`` dataclass. Lists/dicts at the value level are
    compared by canonical-JSON equality, then dumped as short
    previews so the renderer can show "images: [+3, -1]".

    Top-level keys with section semantics (``basic``, ``pricing``,
    etc.) are unfolded one level so the differ output reads like
    ``basic.name`` / ``pricing.list_price`` instead of one giant
    "basic" blob.
    """
    diffs: list[dict[str, Any]] = []
    current = current or {}
    sections = set(current.keys()) | set(proposed.keys())
    # 'v' is schema version, not user-facing
    sections.discard("v")
    for sec in sorted(sections):
        cur_val = current.get(sec)
        prop_val = proposed.get(sec)
        if cur_val == prop_val:
            continue
        if isinstance(prop_val, dict) or isinstance(cur_val, dict):
            cur_dict = cur_val if isinstance(cur_val, dict) else {}
            prop_dict = prop_val if isinstance(prop_val, dict) else {}
            for k in sorted(set(cur_dict.keys()) | set(prop_dict.keys())):
                c = cur_dict.get(k)
                p = prop_dict.get(k)
                if c == p:
                    continue
                diffs.append({
                    "field": f"{sec}.{k}",
                    "current": _to_string(c),
                    "proposed": _to_string(p),
                    "change_kind": _classify_change(c, p),
                })
        else:
            diffs.append({
                "field": sec,
                "current": _to_string(cur_val),
                "proposed": _to_string(prop_val),
                "change_kind": _classify_change(cur_val, prop_val),
            })
    return diffs


# ─── Section builders ────────────────────────────────────────────────


def _get_dynamic_field_mappings() -> list[dict[str, str]]:
    """Read the operator-configured Item→Shopware customField
    mappings from Shopware Setting. Cached on ``frappe.local`` so
    every per-item canonical build inside a Sync run reads from
    memory. Returns ``[]`` on Medusa-only sites where the doctype
    doesn't exist.

    Each mapping dict has keys ``item_field``, ``shopware_custom_field``
    and ``field_type`` — the canonical only needs the first and last;
    the payload builder consumes ``shopware_custom_field``.
    """
    import frappe
    cache_key = "_psync_dyn_cf_mappings"
    cached = getattr(frappe.local, cache_key, None)
    if cached is not None:
        return cached
    out: list[dict[str, str]] = []
    try:
        settings = frappe.get_cached_doc("Shopware Setting")
        rows = getattr(settings, "item_custom_field_mappings", None) or []
        for row in rows:
            item_field = (getattr(row, "item_field", "") or "").strip()
            shopware_key = (getattr(row, "shopware_custom_field", "") or "").strip()
            field_type = (getattr(row, "field_type", "") or "Text").strip() or "Text"
            if item_field and shopware_key:
                out.append({
                    "item_field": item_field,
                    "shopware_custom_field": shopware_key,
                    "field_type": field_type,
                })
    except Exception:  # noqa: BLE001
        # Setting doctype missing on this site (Medusa-only) or the
        # patch hasn't run yet — silently return empty so the engine
        # behaves as if no mappings were configured.
        pass
    setattr(frappe.local, cache_key, out)
    return out


def _get_shopware_product_defaults() -> dict[str, Any]:
    """Read Shopware Setting's ``default_delivery_time`` /
    ``default_restock_time`` — global fallbacks applied only when an
    Item's own field is empty. Cached on ``frappe.local`` per the same
    pattern as :func:`_get_dynamic_field_mappings`. Returns ``{}`` on
    Medusa-only sites where the doctype doesn't exist.
    """
    import frappe
    cache_key = "_psync_shopware_product_defaults"
    cached = getattr(frappe.local, cache_key, None)
    if cached is not None:
        return cached
    out: dict[str, Any] = {}
    try:
        settings = frappe.get_cached_doc("Shopware Setting")
        out["delivery_time"] = (getattr(settings, "default_delivery_time", "") or "").strip()
        out["restock_time"] = int(getattr(settings, "default_restock_time", 0) or 0)
    except Exception:  # noqa: BLE001
        # Setting doctype missing on this site (Medusa-only) or the
        # patch hasn't run yet — silently return empty so the engine
        # behaves as if no defaults were configured.
        pass
    setattr(frappe.local, cache_key, out)
    return out


def _coerce_dynamic_value(raw: Any, field_type: str) -> Any:
    """Coerce an Item field value to the operator-selected output
    type. ``Skip-If-Empty`` returns ``None`` to signal "omit this
    key entirely" — the caller drops the entry from the canonical
    so an empty Item field doesn't pollute the customFields with a
    falsey value that would override the Shopware default."""
    if field_type == "Skip-If-Empty":
        if raw in (None, "", 0, False, [], {}):
            return None
        return raw
    if field_type == "Boolean":
        if raw in (None, "", 0, "0", False, "false", "False"):
            return False
        return True
    if field_type == "Integer":
        try:
            return int(raw or 0)
        except Exception:  # noqa: BLE001
            return 0
    if field_type == "Float":
        try:
            return float(raw or 0)
        except Exception:  # noqa: BLE001
            return 0.0
    # Text (default)
    return _norm_str(str(raw) if raw is not None else "")


def _canonical_dynamic_custom_fields(item) -> dict[str, Any]:
    """Build the dict that gets folded into ``basic.dynamic_custom_fields``.

    Two sources feed the same Shopware ``customFields`` slot:

    1. **Operator-configured Item-field mappings**
       (``Shopware Setting.item_custom_field_mappings``). One row =
       one Item DocType field forwarded to a fixed Shopware
       customField key, with coercion (Boolean / Text / Integer /
       Float / Skip-If-Empty).

    2. **``Item Ecommerce Property`` rows with
       ``property_type = "Custom Field"``** (the in-doctype routing
       flag). The Shopware key is derived from the property name via
       :func:`shopware_custom_field_name` (``Zubehör`` → ``erpnext_zubehör``);
       the value is normalised via :func:`coerce_custom_field_value`
       (recognises ``true`` / ``false`` strings as booleans). No
       mapping-table entry needed — the property's own ``property_type``
       is the routing decision, which keeps the universal property
       table the single source of truth for custom-field-style data
       across channels.

    Keys are the Shopware customField slot names, values are the
    coerced values. Sorted on insertion order is fine —
    ``_serialise`` sorts dict keys at hash time so the hash is
    stable regardless of input ordering.
    """
    out: dict[str, Any] = {}
    # Source 1: operator-configured Item-field mappings.
    for mapping in _get_dynamic_field_mappings():
        raw = getattr(item, mapping["item_field"], None)
        coerced = _coerce_dynamic_value(raw, mapping["field_type"])
        if coerced is None:
            continue  # Skip-If-Empty rule
        out[mapping["shopware_custom_field"]] = coerced

    # Source 2: ecommerce_properties of type "Custom Field".
    from ecommerce_integrations.property_utils import (
        coerce_custom_field_value,
        shopware_custom_field_name,
    )
    for row in _get_ecommerce_properties_for_custom_fields(item):
        pn = (getattr(row, "property_name", None) or "").strip()
        pv = (getattr(row, "property_value", None) or "").strip()
        if not pn or not pv:
            continue
        key = shopware_custom_field_name(pn)
        # Respect the property's declared data type: a ``number`` property
        # (e.g. a per-item shipping cost) must reach the backend as a numeric
        # value so a float customField / Rule Builder can consume it — otherwise
        # it lands as a string and numeric conditions break.
        # Last-write-wins if an operator-configured mapping already
        # set this key — generally these don't collide because
        # Source-1 keys are channel-specific (idealo_feed) while
        # Source-2 keys carry the ``erpnext_`` prefix.
        if (getattr(row, "field_data_type", None) or "") == "number":
            out[key] = _coerce_dynamic_value(pv, "Float")
        else:
            out[key] = coerce_custom_field_value(pv)
    return out


def _get_ecommerce_properties_for_custom_fields(item) -> list:
    """Return Item Ecommerce Property rows that route to Shopware
    customFields (``property_type == "Custom Field"`` and
    ``sync_to_shopware``).

    Reads from the item's already-loaded child table when present —
    the apply-path's full ``frappe.get_doc`` AND the differ-path's
    ``ItemSnapshot`` (BulkContext pre-loads the child rows) both carry
    it. ``None`` (attribute absent entirely) is the only state that
    falls back to a per-item SQL — ad-hoc callers passing a bare
    object. An empty list means "genuinely no rows" and must NOT
    trigger the SQL, otherwise every property-less item pays a
    pointless query per canonical build.
    """
    import frappe
    rows = getattr(item, "ecommerce_properties", None)
    if rows is not None:
        return [
            r for r in rows
            if (getattr(r, "property_type", "") or "") == "Custom Field"
            and int(getattr(r, "sync_to_shopware", 0) or 0)
        ]
    item_code = getattr(item, "item_code", None) or getattr(item, "name", None)
    if not item_code:
        return []
    try:
        return frappe.get_all(
            "Item Ecommerce Property",
            filters={
                "parent": item_code,
                "parenttype": "Item",
                "property_type": "Custom Field",
                "sync_to_shopware": 1,
            },
            fields=["property_name", "property_value", "field_data_type"],
        )
    except Exception:  # noqa: BLE001
        return []


def _canonical_visibilities(item, sync) -> list[dict[str, Any]]:
    """Return the per-item Sales-Channel visibility list.

    Two-tier resolution:

    1. **Resolver primary** — delegate to
       :func:`catalog_mirror.resolver.channels_for_item`, which
       combines per-item ``Ecommerce Channel Override`` (hard
       veto/include), ``Catalog Mirror`` placements and
       ``Smart Collection`` Channel Targets. When the resolver
       returns at least one channel, that wins.

    2. **Sync-aware fallback** — when the resolver returns empty
       AND the operator picked a ``default_sales_channel`` on the
       Sync doc, use the Sync's own configuration:

       * Item is a member of any ``linked_smart_collections`` row
         → broadcast to every ``target_sales_channels`` row (the
         Sync's "primary" channels).
       * Otherwise → broadcast to ``default_sales_channel`` only.

       This is the "alle Items in Medusa, nur Smart-Collection-
       Member auf SSS, rest auf Default" workflow without needing
       the operator to wire ``Ecommerce Smart Collection Target``
       rows per SC. The Sync doc's existing
       ``linked_smart_collections`` + ``target_sales_channels`` +
       new ``default_sales_channel`` field are the single source
       of truth.

    Sorted on ``channel_id`` so the hash is stable. Visibility
    levels follow the Shopware enum (10 link / 20 search / 30
    all); fallback always emits 30. The resolver's primacy means
    operators that DO wire Catalog Mirror or per-item overrides
    keep getting those — the fallback only kicks in for items the
    resolver has no opinion on."""
    backend = (getattr(sync, "backend", "") or "").strip() or "Shopware"
    item_code = getattr(item, "item_code", None) or getattr(item, "name", None)
    if not item_code:
        return []

    out: list[dict[str, Any]] = []
    try:
        from ecommerce_integrations.catalog_mirror.resolver import (
            channels_for_item,
        )
        entries = channels_for_item(item_code, backend) or []
    except Exception:  # noqa: BLE001
        entries = []
    for e in entries:
        sc_id = _norm_str(getattr(e, "sales_channel", "") or "")
        if not sc_id:
            continue
        out.append({
            "channel_id": sc_id,
            "visibility": int(getattr(e, "visibility", 0) or 0),
        })

    if not out:
        # Sync-aware fallback: linked-SC member → target_sales_channels,
        # else → default_sales_channel. Only kicks in when the resolver
        # produced no entries for this item.
        member_codes = _get_linked_sc_member_codes(sync)
        default_sc = (getattr(sync, "default_sales_channel", None) or "").strip()
        if item_code in member_codes:
            for row in (getattr(sync, "target_sales_channels", None) or []):
                sc_id = (getattr(row, "sales_channel_id", "") or "").strip()
                if sc_id:
                    out.append({"channel_id": sc_id, "visibility": 30})
        elif default_sc:
            out.append({"channel_id": default_sc, "visibility": 30})

    out.sort(key=lambda d: d["channel_id"])
    return out


def _get_linked_sc_member_codes(sync) -> frozenset[str]:
    """Item-codes that belong to any of the Sync's
    ``linked_smart_collections``. Cached on ``frappe.local`` keyed
    by sync name so the per-item canonical build inside one run
    reads from memory (the resolve cost is ~one SC walk per linked
    row, 5-50ms each, only paid once per Sync run)."""
    import frappe
    cache_key = f"_psync_linked_sc_members::{getattr(sync, 'name', '')}"
    cached = getattr(frappe.local, cache_key, None)
    if cached is not None:
        return cached
    rows = getattr(sync, "linked_smart_collections", None) or []
    legacy = getattr(sync, "linked_smart_collection", None)
    sc_names: set[str] = {
        (getattr(r, "smart_collection", "") or "").strip()
        for r in rows
    }
    if legacy:
        sc_names.add(legacy.strip())
    sc_names.discard("")
    if not sc_names:
        result = frozenset()
        setattr(frappe.local, cache_key, result)
        return result
    members: set[str] = set()
    try:
        from ecommerce_integrations.smart_collections.engine.resolver import (
            resolve,
        )
        for sc_name in sc_names:
            try:
                sc = frappe.get_cached_doc("Ecommerce Smart Collection", sc_name)
                members |= set(resolve(sc, persist_stats=False) or [])
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        pass
    result = frozenset(members)
    setattr(frappe.local, cache_key, result)
    return result


def _canonical_basic(item, sync) -> dict[str, Any]:
    defaults = _get_shopware_product_defaults()
    out = {
        "name": _norm_str(_render_name(item, sync)),
        "sku": _norm_str(item.item_code),
        "description": _norm_str(_render_description(item, sync)),
        "ean": _norm_str(
            getattr(item, "ean", "") or _from_barcodes(item),
        ),
        "is_active": not bool(getattr(item, "disabled", 0)),
        "uom": _norm_str(getattr(item, "stock_uom", "")),
        # Free-form delivery promise string (e.g. "10-15 Tage").
        # Backend adapters resolve this to their native entity (Shopware's
        # ``delivery_time`` uuid) when pushing — hashing the source string
        # is enough to detect drift without coupling canonical to any
        # backend's id space. Falls back to Shopware Setting's
        # ``default_delivery_time`` when the Item's own field is empty —
        # the Item value always wins when set.
        "delivery_time": (
            _norm_str(getattr(item, "delivery_time", ""))
            or _norm_str(defaults.get("delivery_time", ""))
        ),
        # AI-generated content that lives on Item custom fields.
        # The Shopware adapter projects each non-empty value onto its
        # corresponding ``customFields`` slot (the storefront block
        # for "benefits" / "short description" / SEO meta reads
        # those). Hashing them here means a Gemini re-run that
        # rewrites benefits/short propagates as a delta — without
        # this, only the long description (which feeds
        # ``basic.description``) would trigger drift.
        "ai_short_description": _norm_str(getattr(item, "ai_short_description", "")),
        "ai_benefits": _norm_str(getattr(item, "ai_benefits", "")),
        "ai_seo_description": _norm_str(getattr(item, "ai_seo_description", "")),
        # YouTube URL was hardcoded here pre-2026-05-30, then migrated
        # to flow through ``Item Ecommerce Property`` (property_type
        # "Custom Field", property_name "YouTube Video URL") so it
        # routes via ``_canonical_dynamic_custom_fields`` like every
        # other operator-configurable customField. The shopware key
        # ``erpnext_youtube_video_url`` is preserved via
        # :func:`shopware_custom_field_name`.
    }
    # Minimum order quantity → Shopware ``minPurchase``/``purchaseSteps``
    # (carton-based ordering). Sparse: ERPNext defaults the field to 0
    # and a MOQ of 1 is no constraint, so neither is hashed — see the
    # v5 note on PAYLOAD_VERSION.
    try:
        moq = float(getattr(item, "min_order_qty", 0) or 0)
    except (TypeError, ValueError):
        moq = 0.0
    if moq > 1:
        out["min_order_qty"] = _normalize_float(moq)

    # Days until back-in-stock (Shopware's ``restockTime``, shown as
    # "Nachbestellung in X Tagen"). Sparse — 0 means "no restock time
    # set", so hashing every item's implicit 0 would be noise. Falls
    # back to Shopware Setting's ``default_restock_time`` when the
    # Item's own field is empty/0.
    try:
        restock_time = int(getattr(item, "restock_time", 0) or 0)
    except (TypeError, ValueError):
        restock_time = 0
    if not restock_time:
        restock_time = int(defaults.get("restock_time", 0) or 0)
    if restock_time > 0:
        out["restock_time"] = restock_time

    # Weight (native ERPNext field, kg) and physical dimensions (WeClapp-
    # origin custom fields — not declared by this plugin, read
    # defensively). Shopware expects L/W/H in millimetres; ERPNext's
    # legacy WeClapp fields store centimetres, hence the *10. Sparse:
    # items without a weight/without the WeClapp fields keep a stable
    # hash rather than hashing an implicit 0.
    try:
        weight = float(getattr(item, "weight_per_unit", 0) or 0)
    except (TypeError, ValueError):
        weight = 0.0
    if weight > 0:
        out["weight"] = _normalize_float(weight)
    for canonical_key, fieldname in (
        ("height", "item_height"),
        ("width", "item_width"),
        ("length", "item_length"),
    ):
        if hasattr(item, fieldname):
            try:
                dim_cm = float(getattr(item, fieldname, 0) or 0)
            except (TypeError, ValueError):
                dim_cm = 0.0
            if dim_cm > 0:
                out[canonical_key] = _normalize_float(dim_cm * 10)

    # Closeout / Abverkauf — WeClapp-origin custom field, read
    # defensively. Only hashed when truthy so items without the field
    # (or with it unset) don't drift on every run.
    if bool(getattr(item, "abverkauf", False)):
        out["is_closeout"] = True
    return out


def _render_name(item, sync) -> str:
    """Render the sync's name_template. If empty or no template tag,
    falls back to ``item.item_name``."""
    template = (getattr(sync, "name_template", None) or "").strip()
    if not template:
        return item.item_name or item.item_code
    if "{{" not in template and "{%" not in template:
        return template
    try:
        import frappe
        return frappe.render_template(template, {"item": item.as_dict()})
    except Exception:
        return item.item_name or item.item_code


def _render_description(item, sync) -> str:
    """Resolve the outbound product description from the Sync's source.

    Three modes:

    - ``custom_template`` — render the Sync's ``description_template``
      against the Item via Frappe's sandboxed Jinja.
    - ``ai_generated`` — read ``item.ai_long_description`` (set by the
      AI Description module's ``update_item_with_description``).
      Falls back to ``item.description`` when the AI has not yet
      processed the Item, so the Sync still produces *something*
      rather than blanking the backend's existing copy.
    - default / ``item_description`` — return ``item.description``.

    No I/O in this function: the AI module persists its output onto
    the Item ahead of time (either by user action or by the Sync's
    pre-pass). Reading is a cheap attribute lookup.
    """
    source = (getattr(sync, "description_source", None) or "item_description").strip()
    if source == "custom_template":
        template = (getattr(sync, "description_template", None) or "").strip()
        if not template:
            return ""
        if "{{" not in template and "{%" not in template:
            return template
        try:
            import frappe
            return frappe.render_template(template, {"item": item.as_dict()})
        except Exception:
            return ""
    if source == "ai_generated":
        ai_long = (getattr(item, "ai_long_description", "") or "").strip()
        if ai_long:
            return ai_long
        # AI not available — consult the operator-configured fallback.
        # Default is ``item_description`` for backwards compatibility
        # with the older single-chain behaviour.
        fallback = (getattr(sync, "description_fallback", "") or "item_description").strip()
        if fallback == "empty":
            return ""
        if fallback == "ai_short_description":
            return (getattr(item, "ai_short_description", "") or "").strip()
        if fallback == "custom_template":
            tpl = (getattr(sync, "description_fallback_template", "") or "").strip()
            if not tpl:
                return ""
            if "{{" not in tpl and "{%" not in tpl:
                return tpl
            try:
                import frappe
                return frappe.render_template(
                    tpl, {"item": item.as_dict(), "sync": sync},
                ) or ""
            except Exception:  # noqa: BLE001
                return ""
        # ``item_description`` (default) — same legacy chain.
        return getattr(item, "description", "") or ""
    return getattr(item, "description", "") or ""


def _canonical_pricing(item, sync, ctx=None) -> dict[str, Any]:
    """Resolve price per channel based on the sync's strategy.

    Uses ``ctx.price_for`` when available (O(1) dict lookup over the
    pre-loaded ``tabItem Price`` snapshot); falls back to per-call SQL
    when no context was passed (single-item callers in test code).
    """
    import frappe

    strategy = getattr(sync, "price_strategy", "channel_price_list") or "channel_price_list"
    standard_rate = _normalize_float(getattr(item, "standard_rate", 0) or 0)
    currency = getattr(item, "currency", "") or frappe.db.get_default("currency") or "EUR"

    def _price(price_list: str) -> float | None:
        if not price_list:
            return None
        if ctx is not None:
            return ctx.price_for(item.item_code, price_list)
        return _lookup_item_price(item.item_code, price_list, currency)

    if strategy == "item_standard_rate":
        base = standard_rate
        channel_prices = []
    elif strategy == "custom_markup":
        pct = float(getattr(sync, "markup_percent", 0) or 0)
        base = _normalize_float(standard_rate * (1.0 + pct / 100.0))
        channel_prices = []
    else:  # channel_price_list
        global_list = getattr(sync, "price_list_override", None)
        base_list = global_list
        base = _price(base_list) if base_list else standard_rate
        channel_prices = []
        for row in (getattr(sync, "target_sales_channels", []) or []):
            sc_id = _norm_str(row.sales_channel_id)
            pl = getattr(row, "override_price_list", None) or base_list
            if not sc_id or not pl:
                continue
            channel_prices.append({
                "channel_id": sc_id,
                "price": _normalize_float(_price(pl) or 0),
            })
        channel_prices.sort(key=lambda d: d["channel_id"])

    # Tax rate resolution mirrors ``_canonical_taxes`` so the pricing
    # gross/net derivation lines up exactly with what we hash under
    # ``taxes``. 19 % fallback matches ``payload.py``.
    tax_rate_pct = _max_tax_rate_from_item(item)
    if not tax_rate_pct:
        tax_rate_pct = _max_tax_rate_from_template(getattr(sync, "tax_template", ""))
    if not tax_rate_pct:
        tax_rate_pct = 19.0  # German default — same as payload builder

    # Normalise to gross so canonical + live hash on the same basis.
    # Tax-mode is per-Price-List in real installs (one B2B net list +
    # one B2C gross list is common). Resolution order:
    #
    # 1. ``Price List.custom_price_includes_tax`` — the custom field
    #    installed by ``add_price_list_tax_flag``. Wins per Item Price.
    # 2. ``Shopware Setting.default_price_list_includes_tax`` — global
    #    fallback. Used when ``price_strategy=item_standard_rate``
    #    (no Price List is involved) or when the per-list flag is unset.
    erp_is_gross = False
    resolved_list = None
    if strategy == "channel_price_list":
        # Pick the same list the base price came from (matches the
        # ``_price()`` lookup above).
        resolved_list = (
            getattr(sync, "price_list_override", None) or ""
        )
    if resolved_list:
        try:
            pl_flag = frappe.db.get_value(
                "Price List", resolved_list, "custom_price_includes_tax",
            )
            if pl_flag is not None:
                erp_is_gross = bool(int(pl_flag or 0))
                resolved_list = "_resolved"  # mark as found
        except Exception:  # noqa: BLE001
            resolved_list = None
    if resolved_list != "_resolved":
        # Either no PL involved or per-PL flag absent — global default.
        try:
            setting = frappe.get_single("Shopware Setting")
            erp_is_gross = bool(int(
                getattr(setting, "default_price_list_includes_tax", 0) or 0,
            ))
        except Exception:  # noqa: BLE001
            pass

    base_net_raw = _normalize_float(base or 0)
    if erp_is_gross:
        gross_price = base_net_raw
        # Reverse-derive net from gross so backends that prefer net
        # (Medusa: its pricing module computes gross via tax_rate
        # configured per region) still get a consistent value.
        net_price = round(base_net_raw / (1.0 + tax_rate_pct / 100.0), 2)
    else:
        # Round to currency precision (2 decimals) instead of the
        # 4-decimal canonical default. Shopware's gross prices round
        # the same way; a 4-decimal canonical would chase sub-cent
        # rounding errors (25.50 × 1.19 = 30.345, ERP-B2C list and
        # Shopware both store 30.34) and flag every Item as drift.
        gross_price = round(base_net_raw * (1.0 + tax_rate_pct / 100.0), 2)
        net_price = round(base_net_raw, 2)

    out: dict[str, Any] = {
        "currency": _norm_str(currency),
        # Gross is what Shopware stores (the storefront-facing price
        # incl. tax). Net is what Medusa's pricing module wants
        # (Medusa applies tax at display time via the configured
        # region tax_rate, which gives consistent tax-on-sum
        # rounding for multi-line orders).
        "base_price": gross_price,
        "base_net": net_price,
        "channel_prices": channel_prices,
        "tax_rate_pct": _normalize_float(tax_rate_pct),
    }

    # UVP / Streichpreis (MSRP strike-through). Lookup is conditional
    # — only emitted into the canonical when UVP > sale price (gross,
    # like-for-like). That keeps the hash stable for the ~99 % of
    # items without an MSRP set, so adding this section only flips
    # the hash on items that actually need a strike-through push.
    list_price_list = _resolve_list_price_list_name()
    if list_price_list:
        uvp_raw = _price(list_price_list)
        if uvp_raw and uvp_raw > 0:
            uvp_gross = _normalize_list_price_gross(
                uvp_raw, tax_rate_pct,
            )
            if uvp_gross > gross_price:
                out["list_price"] = round(uvp_gross, 2)
    return out


def _resolve_list_price_list_name() -> str:
    """Read the UVP/Streichpreis price list name from Shopware Setting.

    Cached on ``frappe.local`` for the lifetime of a request/job so
    every per-item canonical build inside one Sync run reads from
    memory. Silent fallback to empty string when the doctype isn't
    installed (Medusa-only sites)."""
    import frappe
    cache_key = "_psync_list_price_list"
    cached = getattr(frappe.local, cache_key, None)
    if cached is not None:
        return cached
    try:
        name = (frappe.db.get_single_value(
            "Shopware Setting", "list_price_price_list",
        ) or "").strip()
    except Exception:  # noqa: BLE001
        name = ""
    setattr(frappe.local, cache_key, name)
    return name


def _normalize_list_price_gross(raw_price: float, tax_rate_pct: float) -> float:
    """Convert the raw UVP price-list rate to gross, honouring the
    ``Shopware Setting.list_price_includes_tax`` flag."""
    import frappe
    cache_key = "_psync_list_price_is_gross"
    cached = getattr(frappe.local, cache_key, None)
    if cached is None:
        try:
            cached = bool(int(
                frappe.db.get_single_value(
                    "Shopware Setting", "list_price_includes_tax",
                ) or 0,
            ))
        except Exception:  # noqa: BLE001
            cached = False
        setattr(frappe.local, cache_key, cached)
    if cached:
        return _normalize_float(raw_price)
    return round(raw_price * (1.0 + tax_rate_pct / 100.0), 2)


def _lookup_item_price(item_code: str, price_list: str, currency: str) -> float | None:
    """Fallback per-item SQL lookup used when no :class:`BulkContext`
    is provided. The column on tabItem Price is ``price_list_rate`` —
    there is no bare ``rate`` column.
    """
    import frappe

    rows = frappe.db.sql(
        """SELECT price_list_rate FROM `tabItem Price`
           WHERE item_code = %s AND price_list = %s
           ORDER BY modified DESC LIMIT 1""",
        (item_code, price_list),
    )
    if rows:
        return float(rows[0][0] or 0)
    return None


def _canonical_inventory(item, sync, ctx=None) -> dict[str, Any]:
    """Sum stock across warehouses. Uses ``ctx.stock_for`` for O(1)
    when running over a bulk pre-load; falls back to per-item SQL
    otherwise."""
    if ctx is not None:
        return {"qty": _normalize_float(ctx.stock_for(item.item_code))}

    import frappe

    qty = frappe.db.sql(
        """SELECT COALESCE(SUM(actual_qty), 0)
           FROM `tabBin` WHERE item_code = %s""",
        (item.item_code,),
    )
    total = float(qty[0][0]) if qty else 0.0
    return {"qty": _normalize_float(total)}


def _file_proxy_token(url: str) -> str:
    """Extract the File docname from a ``/file/<docname>/<filename>`` proxy URL.

    This is the external-storage proxy form a routed/CDN-backed File is served
    under; the first path segment is ``file`` and the second is the File's
    docname (a unique hash). Returns ``""`` for the standard ``/files/<name>``
    form, private files or absolute URLs — those dedup fine by string compare.
    """
    parts = (url or "").split("?", 1)[0].strip("/").split("/")
    if len(parts) >= 2 and parts[0] == "file" and parts[1]:
        return parts[1]
    return ""


def _canonical_images(item, sync, ctx=None) -> list[dict[str, Any]]:
    """Return sorted list of image references.

    ``ctx.images_for`` returns pre-loaded URLs from the bulk File
    snapshot. The Item's own ``image`` field (primary) is also
    included regardless of ctx — it lives directly on the item doc.
    """
    images: list[dict[str, Any]] = []
    primary = _norm_str(getattr(item, "image", ""))
    if primary:
        images.append({"url": primary, "primary": True})

    extra_urls: list[str] = []
    if ctx is not None:
        extra_urls = list(ctx.images_for(item.item_code))
    else:
        import frappe

        rows = frappe.get_all(
            "File",
            filters={
                "attached_to_doctype": "Item",
                "attached_to_name": item.item_code,
                "is_private": 0,
                "is_folder": 0,
            },
            fields=["file_url"],
            order_by="creation asc",
        )
        extra_urls = [(r.get("file_url") or "").strip() for r in rows]

    for url in extra_urls:
        u = _norm_str(url)
        if not u or u == primary:
            continue
        # Same-File-as-primary collapse: ``Item.image`` (the primary) is often
        # a CDN-/proxy-routed URL of one of the attached Files, so a plain URL
        # string compare misses it and the same image gets pushed twice (once
        # as the cover, once as a gallery entry). When an attachment is served
        # via the ``/file/<File-docname>/<name>`` proxy form, that docname is a
        # unique, unguessable hash; if it also appears inside the primary URL,
        # the two reference the same File — skip the duplicate gallery entry.
        tok = _file_proxy_token(u)
        if tok and primary and tok in primary:
            continue
        images.append({"url": u, "primary": False})

    # Deterministic order = display order: the primary (Item.image)
    # first, then attachments in File-creation order. The list is
    # hashed in-order, so re-ordering images in ERPNext flips the
    # ``images`` section hash and the delta path re-pushes the new
    # gallery sequence. (Pre-2026-06 this sorted by URL, which made
    # the storefront order random and re-orders undetectable.)
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for img in images:
        if img["url"] in seen:
            continue
        seen.add(img["url"])
        deduped.append(img)
    return deduped


def _load_property_catalog() -> dict[str, dict[str, Any]]:
    """Return the ``Ecommerce Property Group`` catalog as a dict.

    Shape::

        {
          property_name: {
            "display_order": int,
            "property_type": "Property" | "Text" | "Custom Field",
            "filterable": bool,
            "sync_to_shopware": bool,
            "sync_to_medusa": bool,
            "field_data_type": str,
            "options": {value_string: option_display_order, ...},
          },
          ...
        }

    Memoised on ``frappe.local`` for the lifetime of one job — a cron
    tick over 30 k items loads the 327-group catalog exactly once
    instead of once-per-item. The Group catalog is small (a few KB)
    and changes on operator action (rare), so a per-job snapshot is
    fine; the next job picks up edits.

    Missing-catalog fallback: when the doctype hasn't been migrated
    yet (e.g. a fresh checkout running tests before patches), returns
    an empty dict and callers fall back to ``property_priority`` on
    raw names. This keeps the unit tests independent of the catalog.
    """
    import frappe

    cache_key = "_psync_property_catalog"
    cached = getattr(frappe.local, cache_key, None)
    if cached is not None:
        return cached

    out: dict[str, dict[str, Any]] = {}
    try:
        groups = frappe.db.sql(
            """
            SELECT name AS property_name,
                   COALESCE(display_order, 800) AS display_order,
                   COALESCE(property_type, 'Property') AS property_type,
                   COALESCE(filterable, 0) AS filterable,
                   COALESCE(sync_to_shopware, 0) AS sync_to_shopware,
                   COALESCE(sync_to_medusa, 0) AS sync_to_medusa,
                   COALESCE(field_data_type, 'text') AS field_data_type
            FROM `tabEcommerce Property Group`
            """,
            as_dict=True,
        )
        for g in groups:
            out[g["property_name"]] = {
                "display_order": int(g["display_order"] or 800),
                "property_type": g["property_type"],
                "filterable": bool(int(g["filterable"] or 0)),
                "sync_to_shopware": bool(int(g["sync_to_shopware"] or 0)),
                "sync_to_medusa": bool(int(g["sync_to_medusa"] or 0)),
                "field_data_type": g["field_data_type"],
                "options": {},
            }
        opts = frappe.db.sql(
            """
            SELECT parent AS group_name, value, COALESCE(display_order, 0) AS display_order
            FROM `tabEcommerce Property Option`
            """,
            as_dict=True,
        )
        for o in opts:
            grp = out.get(o["group_name"])
            if grp is not None:
                grp["options"][o["value"]] = int(o["display_order"] or 0)
    except Exception:  # noqa: BLE001
        out = {}

    setattr(frappe.local, cache_key, out)
    return out


def _canonical_properties(item, sync) -> dict[str, Any]:
    """Brand, manufacturer, Item Attribute values, and ecommerce properties."""
    import frappe
    from ecommerce_integrations.product_sync.engine.property_classifier import (
        property_priority,
    )

    out: dict[str, Any] = {
        "brand": _norm_str(getattr(item, "brand", "")),
        "manufacturer": _norm_str(getattr(item, "manufacturer", "")),
    }
    attrs = []
    for row in (getattr(item, "attributes", []) or []):
        name = _norm_str(getattr(row, "attribute", ""))
        value = _norm_str(getattr(row, "attribute_value", ""))
        if name and value:
            attrs.append({"name": name, "value": value})
    attrs.sort(key=lambda d: (d["name"], d["value"]))
    out["attributes"] = attrs

    # Include ecommerce_properties flagged for THIS sync's backend.
    # Rows with ``property_type = "Custom Field"`` go through
    # :func:`_canonical_dynamic_custom_fields` into the
    # ``basic.dynamic_custom_fields`` section instead, so they end
    # up in the product's ``customFields`` JSON column rather than
    # the filterable-property m2m.
    #
    # Source of truth for ``property_type`` / sync-flags / filterable /
    # display-position is the ``Ecommerce Property Group`` catalog —
    # the per-row legacy fields (kept for rollback during the
    # migration window) are ignored here. Rows whose group is missing
    # from the catalog (operator added a property name and hasn't
    # seeded a Group yet) fall back to ``property_priority`` for
    # ordering and default to ``Property`` type + sync-on for both
    # backends, matching the old behaviour.
    #
    # Backend-aware filter: each Group carries an independent
    # ``sync_to_shopware`` / ``sync_to_medusa`` toggle. A Sync doc has
    # exactly one backend, so we read the matching flag — a property
    # marked "Shopware only" doesn't enter a Medusa Sync's canonical
    # (and vice versa). Same item under both Syncs therefore produces
    # two canonicals that differ only by which properties they
    # include; each Sync hashes its own.
    #
    # Sort key ``(group_order, group_name, option_order, value)``
    # mirrors the Shopware ``property_group.position`` /
    # ``property_group_option.position`` model: a single global
    # position per group + per option. The positions are embedded in
    # each row dict so a catalog edit flips every affected item's
    # hash → next sync re-pushes the new order.
    backend = (getattr(sync, "backend", "") or "Shopware").strip().lower()
    sync_flag = "sync_to_medusa" if backend == "medusa" else "sync_to_shopware"

    catalog = _load_property_catalog()
    # ``None`` = child table not loaded on this object → per-item SQL
    # fallback. An empty list means "no rows" and skips the SQL — the
    # BulkContext pre-loads the rows for every snapshot, so the
    # differ-path stays free of per-item queries.
    in_mem = getattr(item, "ecommerce_properties", None)
    # (name, value, property_type, per-row backend sync flag). The row
    # flag only gates UNCATALOGUED rows — catalogued rows are gated by
    # their Group's flag below.
    raw_rows: list[tuple[str, str, str, int]] = []
    if in_mem is not None:
        for row in in_mem:
            pn = _norm_str(getattr(row, "property_name", ""))
            pv = _norm_numeric_value(_norm_str(getattr(row, "property_value", "")))
            if pn and pv:
                raw_rows.append((
                    pn, pv,
                    _norm_str(getattr(row, "property_type", "")),
                    int(getattr(row, sync_flag, 0) or 0),
                ))
    else:
        try:
            rows = frappe.get_all(
                "Item Ecommerce Property",
                filters={
                    "parent": item.item_code,
                    "parenttype": "Item",
                },
                fields=["property_name", "property_value", "property_type",
                        sync_flag],
            )
            for r in rows:
                pn = _norm_str(r.get("property_name"))
                pv = _norm_numeric_value(_norm_str(r.get("property_value")))
                if pn and pv:
                    raw_rows.append((
                        pn, pv, _norm_str(r.get("property_type")),
                        int(r.get(sync_flag) or 0),
                    ))
        except Exception:  # noqa: BLE001
            pass

    ecom_props: list[dict[str, Any]] = []
    # Custom-field rows flagged for THIS backend. "Custom Field" type
    # marks machine/integration data, not a customer-facing spec, so it
    # never enters the display ``ecommerce_properties`` list (and, for
    # Shopware, never the filterable ``properties`` m2m — that would
    # reference option entities the Phase-A.5 ensure step skips for
    # Custom-Field rows, FK-violating the bulk upsert). Each backend's
    # payload builder transforms these for its own model: Shopware →
    # ``customFields`` JSON column (via ``basic.dynamic_custom_fields``),
    # Medusa → top-level product ``metadata`` keys. Without surfacing
    # them here a row marked ``sync_to_medusa`` but typed Custom Field
    # reaches NEITHER channel.
    custom_fields: dict[str, str] = {}
    for pn, pv, row_ptype, row_flag in raw_rows:
        group = catalog.get(pn)
        if group is None:
            # Property not in catalog yet — fall back to the ROW's own
            # ``property_type`` (the in-doctype routing flag) and the
            # row's own backend sync flag. Ordering falls back to the
            # legacy classifier so operators don't lose data when they
            # import a new SKU before re-running the catalog patch.
            group_order = property_priority(pn)
            ptype = row_ptype or "Property"
            option_order = 0
            flag_ok = bool(row_flag)
        else:
            if not group[sync_flag]:
                continue
            ptype = group["property_type"]
            group_order = group["display_order"]
            option_order = group["options"].get(pv, 99999)
            flag_ok = True  # catalogued rows already passed the gate
        if ptype == "Custom Field":
            if flag_ok:
                custom_fields[pn] = pv
            continue
        if ptype not in ("Property", "Text"):
            continue
        ecom_props.append({
            "name": pn,
            "value": pv,
            "group_order": int(group_order),
            "option_order": int(option_order),
        })
    ecom_props.sort(key=lambda d: (d["group_order"], d["name"], d["option_order"], d["value"]))
    out["ecommerce_properties"] = ecom_props
    if custom_fields:
        out["custom_fields"] = {k: custom_fields[k] for k in sorted(custom_fields)}
    return out


def _canonical_seo(item, sync) -> dict[str, Any]:
    """SEO fields with a three-tier fallback chain.

    For each of ``meta_title`` / ``meta_description`` / ``slug``:

    1. Item field (``meta_title`` / ``meta_description`` / ``seo_slug``).
    2. AI-generated field if present (``ai_seo_title`` /
       ``ai_seo_description``) — populated by the AI Description module
       when ``include_seo`` is enabled there.
    3. Per-Sync Jinja template (``seo_meta_title_template`` /
       ``seo_meta_description_template`` / ``seo_slug_template``)
       rendered in Frappe's sandbox with ``{item, sync, brand}``.

    Render errors are swallowed into an empty string so a broken
    template can never block the differ — the operator sees the blank
    field on the backend and knows where to look. The chain is
    deterministic: same Item + same templates ⇒ same payload ⇒ same
    hash.
    """
    slug = (
        _norm_str(getattr(item, "seo_slug", ""))
        or _norm_str(getattr(item, "slug", ""))
    )
    meta_title = (
        _norm_str(getattr(item, "meta_title", ""))
        or _norm_str(getattr(item, "ai_seo_title", ""))
    )
    meta_desc = (
        _norm_str(getattr(item, "meta_description", ""))
        or _norm_str(getattr(item, "ai_seo_description", ""))
    )

    if not meta_title:
        meta_title = _norm_str(
            _render_seo_template(sync, "seo_meta_title_template", item),
        )
    if not meta_desc:
        meta_desc = _norm_str(
            _render_seo_template(sync, "seo_meta_description_template", item),
        )
    if not slug:
        rendered = _render_seo_template(sync, "seo_slug_template", item)
        if rendered:
            slug = _slugify(rendered)

    return {
        "slug": slug,
        "meta_title": meta_title,
        "meta_description": meta_desc,
    }


def _render_seo_template(sync, fieldname: str, item) -> str:
    """Render a Sync-doc SEO template against ``item`` in the sandbox.

    Returns an empty string on missing template, render error, or
    sandboxed-feature violation. Frappe's ``render_template`` runs
    inside the same Jinja sandbox we use for notifications, so the
    template author can't escape into arbitrary Python.
    """
    template = (getattr(sync, fieldname, "") or "").strip()
    if not template:
        return ""
    try:
        import frappe  # local import — keeps the canonical module
                       # importable from non-Frappe contexts (tests).
        # ``brand`` is optional — only pull when the Sync has a single
        # branding doc resolved; otherwise leave undefined so the
        # template author sees an explicit "brand is undefined" error
        # at design time rather than a silent empty string.
        context: dict[str, Any] = {"item": item, "sync": sync}
        try:
            from ecommerce_integrations.ecommerce_integrations.doctype.ecommerce_channel_branding.ecommerce_channel_branding import (
                get_branding,
            )
            channels = getattr(sync, "target_sales_channels", None) or []
            if channels and len(channels) == 1:
                ch = channels[0]
                channel_name = (
                    getattr(ch, "sales_channel", None)
                    or getattr(ch, "channel", None)
                )
                if channel_name:
                    context["brand"] = get_branding(channel_name)
        except Exception:  # noqa: BLE001
            # Branding is best-effort context. The template can still
            # reference ``brand`` via ``brand|default('')`` and works
            # for sites without the branding doctype installed.
            pass
        return frappe.render_template(template, context) or ""
    except Exception:  # noqa: BLE001
        return ""


def _slugify(value: str) -> str:
    """Lowercase + hyphenate a rendered slug template.

    Matches the slug shape Shopware and Medusa both accept (lowercase
    ASCII, hyphens, no double hyphens, no leading/trailing hyphens).
    Unicode characters are stripped rather than transliterated —
    operators get a clean ASCII slug or no slug at all.
    """
    import re
    s = (value or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s


def _canonical_taxes(item, sync) -> dict[str, Any]:
    """Tax rate the apply step will push as ``taxId``.

    Hashes the percentage (e.g. ``19.0``) — same anchor the live
    side reads off Shopware's ``tax.taxRate``. The Item Tax
    Template *name* is ERPNext-local and would force permanent
    drift if hashed, so we resolve it to the rate first using the
    same item-then-sync chain ``_resolve_tax_rate_pct`` uses.
    """
    rate_pct = _max_tax_rate_from_item(item)
    if not rate_pct:
        rate_pct = _max_tax_rate_from_template(getattr(sync, "tax_template", ""))
    return {"rate_pct": _normalize_float(rate_pct)}


def _max_tax_rate_from_item(item) -> float:
    """Highest non-zero tax_rate across the Item's tax templates.

    ``Item Tax Template`` contains many sub-rows (one per account,
    e.g. "Vorsteuer", "Umsatzsteuer", "innergem. Erwerb", …). Most
    are zero for any given country; the meaningful rate is the
    highest non-zero. Picking the max matches what ERPNext itself
    does at invoice-line time.
    """
    for row in (getattr(item, "taxes", None) or []):
        tpl = (getattr(row, "item_tax_template", "") or "").strip()
        rate = _max_tax_rate_from_template(tpl)
        if rate:
            return rate
    return 0.0


def _max_tax_rate_from_template(template_name: str) -> float:
    """Highest non-zero ``tax_rate`` across the template's child rows.

    Reads ``Item Tax Template.taxes[].tax_rate``. German-style charts
    of accounts ship templates with many child rows (one per tax-
    account: Umsatzsteuer, Vorsteuer, innergem. Erwerb, § 13b UStG,
    etc.) where most rows are 0 % and only one or two carry the
    actual sales-tax rate. Picking the max picks the sales-tax rate
    without having to enumerate per-country accounting conventions.
    """
    if not template_name:
        return 0.0
    try:
        import frappe
        tpl = frappe.get_cached_doc("Item Tax Template", template_name)
    except Exception:  # noqa: BLE001
        return 0.0
    best = 0.0
    for sub in (tpl.taxes or []):
        try:
            rate = float(getattr(sub, "tax_rate", None) or 0)
        except (TypeError, ValueError):
            rate = 0.0
        if rate > best:
            best = rate
    return best


def _canonical_categories(item, sync) -> dict[str, Any]:
    """Backend-category UUIDs the apply step would push.

    Reads ``Item Group.shopware_category_id`` / ``medusa_category_id``
    (whichever matches the Sync's backend) — the same lookup the
    payload builder does at apply time. Hashing the UUIDs (not the
    IG name) means:

    - When the mapping exists and is correct, the hash matches what
      Shopware reports back via the live-side canonical.
    - When no mapping exists yet, ``ids`` is ``[]`` — the apply step
      also skips the ``categories`` field in that case, so the hash
      reflects "we'd push nothing for categories" and the differ
      doesn't flap forever.

    The IG name is kept under a sibling key so the preview UI can
    still show "would link to <name>" without re-reading the Item.
    """
    ig_name = _norm_str(getattr(item, "item_group", ""))
    backend = (getattr(sync, "backend", "") or "").strip()
    ids: list[str] = []
    if ig_name and backend:
        try:
            import frappe
            field = (
                "shopware_category_id" if backend == "Shopware"
                else "medusa_category_id" if backend == "Medusa"
                else None
            )
            if field:
                # ``frappe.get_meta`` cache means this is a cheap check.
                meta = frappe.get_meta("Item Group")
                if any(f.fieldname == field for f in meta.fields):
                    cat_id = frappe.db.get_value("Item Group", ig_name, field)
                    if cat_id:
                        ids = [cat_id]
        except Exception:  # noqa: BLE001 — never crash the hash builder
            pass
    # Only the resolved UUID list goes into the hash. The IG name is
    # ERPNext-local and never appears on the Shopware side, so hashing
    # it would force permanent drift (live always emits "").
    return {"ids": ids}


# ─── Helpers ─────────────────────────────────────────────────────────


def _flag(sync, name: str, *, default: bool) -> bool:
    """Read a sync toggle, treating missing attribute as ``default``."""
    val = getattr(sync, name, None)
    if val is None:
        return default
    return bool(int(val) if isinstance(val, (str, int, float)) else val)


def _norm_str(value) -> str:
    """Normalise to single-space-collapsed stripped string. ``None`` → ``""``."""
    if value is None:
        return ""
    s = str(value)
    if not s:
        return ""
    return " ".join(s.split())


def _norm_numeric_value(value: str) -> str:
    """Collapse a trailing-zero decimal string to its minimal numeric form
    so ``"5.00"`` and ``"5"`` (or ``"344.50"`` and ``"344.5"``) don't produce
    two property options / spec values for one number.

    Only *pure* decimals (``-?<digits>.<digits>``) are rewritten; integers,
    unit-suffixed values (``"750 mm"``), codes and any non-numeric text pass
    through untouched, so this never mangles a label or strips leading zeros
    off an integer code. The display-property path (``_canonical_properties``)
    runs every value through here, so the canonical — and therefore the wire
    payload AND the hash — never carries the denormalised ``"5.00"`` form
    regardless of how messy the source ``Item Ecommerce Property`` row is.
    """
    if not value or "." not in value:
        return value
    body = value[1:] if value[:1] == "-" else value
    intpart, _, fracpart = body.partition(".")
    if not intpart.isdigit() or not fracpart.isdigit():
        return value
    try:
        return format(Decimal(value).normalize(), "f")
    except (InvalidOperation, ValueError):
        return value


def _normalize_float(v) -> float:
    """Round to ``_FLOAT_DECIMALS`` to swallow float-arithmetic noise."""
    if v is None:
        return 0.0
    return round(float(v), _FLOAT_DECIMALS)


def _from_barcodes(item) -> str:
    """Read EAN from the standard ``Item.barcodes`` child table if
    the Item doesn't carry an explicit ``ean`` field. Picks the first
    row with ``barcode_type='EAN'`` or falls back to the first row."""
    rows = getattr(item, "barcodes", []) or []
    if not rows:
        return ""
    for r in rows:
        if (getattr(r, "barcode_type", "") or "").upper() == "EAN":
            return _norm_str(r.barcode)
    return _norm_str(rows[0].barcode)


def _serialise(payload: dict[str, Any]) -> str:
    """JSON-serialise with stable key order and compact separators."""
    return json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=_default,
    )


def _default(obj):
    """JSON-serialiser fallback for non-standard types."""
    if isinstance(obj, Decimal):
        return _normalize_float(float(obj))
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, set):
        return sorted(obj)
    raise TypeError(
        f"Object of type {type(obj).__name__} is not JSON serialisable",
    )


def _to_string(v) -> str | None:
    """Compact preview for diff rendering. Lists/dicts → JSON; None → None."""
    if v is None:
        return None
    if isinstance(v, (str, int, float, bool)):
        return str(v)
    return _serialise(v)


def _classify_change(current, proposed) -> str:
    """Map ``(current, proposed)`` to FieldDiff.change_kind."""
    if current in (None, "", [], {}) and proposed not in (None, "", [], {}):
        return "added"
    if current not in (None, "", [], {}) and proposed in (None, "", [], {}):
        return "removed"
    return "modified"
