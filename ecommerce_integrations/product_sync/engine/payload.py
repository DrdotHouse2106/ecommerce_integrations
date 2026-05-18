"""Adapter-format payload builders.

The differ produces a canonical payload + hash; the apply loop has to
convert that canonical shape into the wire format each backend wants.
This module is the single source of truth for that translation.

The contract: given ``(item_doc, sync_doc, canonical)``, return a
dict ready to hand to ``adapter.upsert_product(payload=...)``. The
adapter does *no* further translation — it only handles auth,
batching, retry.

Phase-5 first cut covers the must-haves (name, sku, description,
active, stock, visibilities, base price). Tax / categories / images
/ properties are listed in the canonical but not yet pushed —
TODOs marked inline. Adding them is a Phase-5.1 follow-up that
doesn't change the apply loop.
"""

from __future__ import annotations

from typing import Any

import frappe


# Shopware product-visibility codes — `30` = "all" (search + listing
# + direct URL access), the maximum-exposure mode.
_SHOPWARE_VISIBILITY_ALL = 30


def build_shopware_payload(
    item, sync, canonical: dict[str, Any], *, external_id: str | None = None,
) -> dict[str, Any]:
    """Build the Shopware product-payload from canonical + sync settings.

    Shopware's ``_action/sync`` accepts the same product entity shape
    as ``POST /api/product`` — flat dict with ``id``, ``name``,
    ``productNumber``, etc. Missing fields are left untouched on the
    server (partial-update semantics).
    """
    basic = canonical.get("basic") or {}
    pricing = canonical.get("pricing") or {}
    inventory = canonical.get("inventory") or {}

    payload: dict[str, Any] = {
        "name": basic.get("name") or item.item_code,
        "productNumber": basic.get("sku") or item.item_code,
        "description": basic.get("description") or "",
        "active": bool(basic.get("is_active", True)),
        "stock": int(inventory.get("qty") or 0),
    }
    if external_id:
        payload["id"] = external_id

    # Visibilities — one row per target sales channel.
    visibilities = []
    for row in (getattr(sync, "target_sales_channels", []) or []):
        sc_id = (row.sales_channel_id or "").strip()
        if not sc_id:
            continue
        visibilities.append({
            "salesChannelId": sc_id,
            "visibility": _SHOPWARE_VISIBILITY_ALL,
        })
    if visibilities:
        payload["visibilities"] = visibilities

    # Pricing: single base-price for the system default currency.
    # Phase-5 keeps this simple — Phase-5.1 adds per-channel prices
    # via the Shopware Rule engine (using sync.auto_create_shopware_rules).
    if int(getattr(sync, "sync_pricing", 0) or 0):
        base_price = pricing.get("base_price")
        currency_code = pricing.get("currency") or _default_currency()
        if base_price is not None:
            currency_id = _resolve_shopware_currency_id(currency_code)
            if currency_id:
                # Shopware wants gross + net. Without a tax-template
                # resolved, set both equal and `linked=True` — Shopware
                # will recompute net based on the product's taxId.
                payload["price"] = [{
                    "currencyId": currency_id,
                    "gross": float(base_price),
                    "net": float(base_price),
                    "linked": True,
                }]

    # Categories: link the product to its Item-Group's backend category
    # (written by Catalog Mirror on its own apply). When the mapping is
    # missing we skip silently here — the pre-flight check warns the
    # operator up-front, so reaching this branch with no mapping means
    # they consciously chose to push without categories.
    category_ids = _resolve_shopware_category_ids(item)
    if category_ids:
        payload["categories"] = [{"id": cid} for cid in category_ids]

    # TODO Phase-5.1: tax_id from sync.tax_template ↔ Shopware tax-rate mapping
    # TODO Phase-5.1: images — currently URL-only (no binary), needs
    #   evaluation of internal_host_patterns + binary fallback
    # TODO Phase-5.1: properties (brand, manufacturer, attributes)
    return payload


def build_medusa_payload(
    item, sync, canonical: dict[str, Any], *, external_id: str | None = None,
) -> dict[str, Any]:
    """Medusa v2 product payload.

    Per the Medusa v2 research: only ``title`` is strictly required;
    everything else is optional. We set ``external_id`` to the ERPNext
    item_code so subsequent upserts can find the row by lookup. POST
    semantics (not PATCH) mean nested arrays REPLACE — variants and
    images are sent as full state.
    """
    basic = canonical.get("basic") or {}
    pricing = canonical.get("pricing") or {}
    inventory = canonical.get("inventory") or {}

    payload: dict[str, Any] = {
        "title": basic.get("name") or item.item_code,
        "handle": _slugify(basic.get("name") or item.item_code),
        "description": basic.get("description") or "",
        "status": "published" if basic.get("is_active") else "draft",
        # external_id is OUR upsert anchor — must always reflect the ERP code.
        "external_id": item.item_code,
        "metadata": {
            "erpnext_item_code": item.item_code,
            "sync_source": sync.name,
        },
    }

    # Single default variant covering the SKU + price + inventory.
    # Multi-variant products are Phase-7.1 territory.
    variant: dict[str, Any] = {
        "title": basic.get("name") or item.item_code,
        "sku": basic.get("sku") or item.item_code,
        "manage_inventory": True,
        "prices": [],
    }
    ean = basic.get("ean")
    if ean:
        variant["ean"] = ean

    if int(getattr(sync, "sync_pricing", 0) or 0):
        base_price = pricing.get("base_price")
        currency = (pricing.get("currency") or "eur").lower()
        if base_price is not None:
            # Medusa stores prices in minor units (cents).
            variant["prices"].append({
                "currency_code": currency,
                # round to int cents
                "amount": int(round(float(base_price) * 100)),
            })

    payload["variants"] = [variant]

    # Sales channels — full replacement (Medusa POST semantics).
    sc_ids = [
        (row.sales_channel_id or "").strip()
        for row in (getattr(sync, "target_sales_channels", []) or [])
    ]
    sc_ids = [s for s in sc_ids if s]
    if sc_ids:
        payload["sales_channels"] = [{"id": s} for s in sc_ids]

    # Categories: same wiring as Shopware — read the Item-Group's
    # ``medusa_category_id`` (written by Catalog Mirror) and ship it
    # in Medusa's ``categories`` array. Missing mappings are silently
    # dropped because the pre-flight check has already warned the
    # operator about the unmapped IGs.
    category_ids = _resolve_medusa_category_ids(item)
    if category_ids:
        payload["categories"] = [{"id": cid} for cid in category_ids]

    # TODO Phase-7.1: variants[].inventory_items linking (full Inventory module)
    # TODO Phase-7.1: images[] (url-only push; CDN-side)
    # TODO Phase-7.1: tags / type from properties.brand etc.
    return payload


# ─── Helpers ─────────────────────────────────────────────────────────


def _resolve_shopware_category_ids(item) -> list[str]:
    """Look up the Shopware-category UUID for an Item's Item Group.

    Reads ``Item Group.shopware_category_id`` — populated by Catalog
    Mirror's apply step. Returns ``[]`` if the Item has no item_group
    or the IG has no mapping (e.g. Catalog Mirror never ran). Failures
    are swallowed so a single broken row can't crash the apply loop.
    """
    ig = getattr(item, "item_group", None)
    if not ig:
        return []
    try:
        cat_id = frappe.db.get_value(
            "Item Group", ig, "shopware_category_id",
        )
    except Exception:  # noqa: BLE001
        return []
    return [cat_id] if cat_id else []


def _resolve_medusa_category_ids(item) -> list[str]:
    """Mirror of :func:`_resolve_shopware_category_ids` for Medusa.

    Reads ``Item Group.medusa_category_id`` (Catalog Mirror writes
    this in the Medusa apply path). Same swallow-and-return-empty
    semantics so a broken row doesn't stop the rest of the push.
    """
    ig = getattr(item, "item_group", None)
    if not ig:
        return []
    try:
        cat_id = frappe.db.get_value(
            "Item Group", ig, "medusa_category_id",
        )
    except Exception:  # noqa: BLE001
        return []
    return [cat_id] if cat_id else []


def _default_currency() -> str:
    try:
        return frappe.db.get_default("currency") or "EUR"
    except Exception:
        return "EUR"


def _resolve_shopware_currency_id(code: str) -> str | None:
    """Map an ISO currency code (EUR, USD, …) to the Shopware
    ``currency.id`` UUID. Cached per-request.

    The lookup is done via the Shopware Setting's cached values where
    possible; falls back to a per-call API hit. Phase-5 keeps it
    minimal — Phase-5.1 will add a proper currency map on the Setting.
    """
    if not code:
        return None
    code = code.upper().strip()
    cache_key = f"_psync_shopware_currency:{code}"
    cached = frappe.cache().get_value(cache_key)
    if cached:
        return cached
    try:
        from ecommerce_integrations.shopware6.connection import temp_shopware_session

        @temp_shopware_session
        def _lookup(client):
            resp = client.request_post(
                "search/currency",
                payload={
                    "limit": 1,
                    "filter": [{"type": "equals", "field": "isoCode", "value": code}],
                },
            )
            data = (resp or {}).get("data") or []
            if data:
                return (data[0].get("id") or "").strip() or None
            return None

        value = _lookup()
        if value:
            frappe.cache().set_value(cache_key, value, expires_in_sec=3600)
        return value
    except Exception:
        return None


def _slugify(s: str) -> str:
    """Cheap slug for Medusa handle field. Medusa auto-slugs if we
    don't send one, but explicit-slug helps with idempotent upserts."""
    import re
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-") or "product"
