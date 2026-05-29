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
from decimal import Decimal
from typing import Any

# Schema version. Bump only on backward-incompatible changes to the
# payload shape (added/removed top-level keys, changed semantics). Do
# NOT bump for "we now collect a new property" — that is data drift,
# not schema drift, and will naturally produce different hashes.
PAYLOAD_VERSION = 2

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


def _canonical_basic(item, sync) -> dict[str, Any]:
    return {
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
        # backend's id space.
        "delivery_time": _norm_str(getattr(item, "delivery_time", "")),
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
        "youtube_video_url": _norm_str(getattr(item, "youtube_video_url", "")),
    }


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

    base_net = _normalize_float(base or 0)
    if erp_is_gross:
        gross_price = base_net
    else:
        # Round to currency precision (2 decimals) instead of the
        # 4-decimal canonical default. Shopware's gross prices round
        # the same way; a 4-decimal canonical would chase sub-cent
        # rounding errors (25.50 × 1.19 = 30.345, ERP-B2C list and
        # Shopware both store 30.34) and flag every Item as drift.
        gross_price = round(base_net * (1.0 + tax_rate_pct / 100.0), 2)

    out: dict[str, Any] = {
        "currency": _norm_str(currency),
        "base_price": gross_price,
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
        )
        extra_urls = [(r.get("file_url") or "").strip() for r in rows]

    for url in extra_urls:
        u = _norm_str(url)
        if not u or u == primary:
            continue
        images.append({"url": u, "primary": False})

    # Deterministic order.
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for img in sorted(images, key=lambda d: d["url"]):
        if img["url"] in seen:
            continue
        seen.add(img["url"])
        deduped.append(img)
    return deduped


def _canonical_properties(item, sync) -> dict[str, Any]:
    """Brand, manufacturer, Item Attribute values, and ecommerce properties."""
    import frappe

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

    # Include ecommerce_properties flagged for Shopware sync.
    # Sorted by (name, value) for stable hash comparison with the
    # live Shopware side (properties have no ordering in Shopware).
    ecom_props = []
    try:
        rows = frappe.get_all(
            "Item Ecommerce Property",
            filters={
                "parent": item.item_code,
                "parenttype": "Item",
                "sync_to_shopware": 1,
            },
            fields=["property_name", "property_value"],
        )
        for r in rows:
            pn = _norm_str(r.get("property_name"))
            pv = _norm_str(r.get("property_value"))
            if pn and pv:
                ecom_props.append({"name": pn, "value": pv})
    except Exception:  # noqa: BLE001
        pass
    ecom_props.sort(key=lambda d: (d["name"], d["value"]))
    out["ecommerce_properties"] = ecom_props
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
