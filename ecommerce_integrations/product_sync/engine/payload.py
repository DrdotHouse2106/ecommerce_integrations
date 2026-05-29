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
    item,
    sync,
    canonical: dict[str, Any],
    *,
    external_id: str | None = None,
    changed_sections: set[str] | None = None,
) -> dict[str, Any]:
    """Build the Shopware product-payload from canonical + sync settings.

    Shopware's ``_action/sync`` accepts the same product entity shape
    as ``POST /api/product`` — flat dict with ``id``, ``name``,
    ``productNumber``, etc. Missing fields are left untouched on the
    server (partial-update semantics).

    When ``changed_sections`` is supplied the function emits ONLY the
    payload keys whose source canonical section is in the set. This
    keeps update payloads minimal — a delivery-time-only change pushes
    ``{id, deliveryTime}`` instead of every field including images,
    prices, categories, etc. Pass ``None`` (the default) to keep the
    legacy full-payload behaviour, which is what fresh creates need.

    The mapping from canonical section to payload key follows the
    section builders in :mod:`canonical`:

    - ``basic``      → name, productNumber, description, active,
                       deliveryTime
    - ``inventory``  → stock
    - ``pricing``    → price, taxId
    - ``categories`` → categories
    - ``properties`` → manufacturer
    - ``parentId`` is always emitted on UPDATE so the variant→template
      link survives a partial push (no canonical section drives it).
    """
    full = changed_sections is None

    def _wants(section: str) -> bool:
        return full or section in (changed_sections or set())
    basic = canonical.get("basic") or {}
    pricing = canonical.get("pricing") or {}
    inventory = canonical.get("inventory") or {}

    payload: dict[str, Any] = {}
    if _wants("basic"):
        payload["name"] = basic.get("name") or item.item_code
        payload["productNumber"] = basic.get("sku") or item.item_code
        payload["description"] = basic.get("description") or ""
        payload["active"] = bool(basic.get("is_active", True))
    if _wants("inventory"):
        payload["stock"] = int(inventory.get("qty") or 0)
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
    # ``canonical.base_price`` is always normalised to GROSS by
    # ``_canonical_pricing`` (net→gross conversion already applied
    # there when ``erp_is_gross=False``). We derive net from that
    # gross price so Shopware receives both sides correctly.
    if int(getattr(sync, "sync_pricing", 0) or 0) and _wants("pricing"):
        base_price = pricing.get("base_price")
        currency_code = pricing.get("currency") or _default_currency()
        if base_price is not None:
            currency_id = _resolve_shopware_currency_id(currency_code)
            if currency_id:
                tax_rate_pct = _resolve_tax_rate_pct(sync, item=item)
                gross, net = _compute_gross_net(
                    float(base_price),
                    tax_rate_pct,
                    erp_is_gross=True,
                )
                price_row: dict[str, Any] = {
                    "currencyId": currency_id,
                    "gross": gross,
                    "net": net,
                    # ``linked=True`` makes Shopware re-derive one side
                    # from the other whenever its product taxId changes,
                    # so a future tax update on the storefront stays
                    # consistent without a re-push.
                    "linked": True,
                }
                # UVP / Streichpreis. Canonical only emits ``list_price``
                # when it strictly exceeds ``base_price`` (gross), so a
                # presence-check here is sufficient — no extra comparison
                # needed. Both sides are gross, derive net via the same
                # tax-rate the base price uses for symmetry.
                list_price_gross = pricing.get("list_price")
                if list_price_gross:
                    lp_gross, lp_net = _compute_gross_net(
                        float(list_price_gross),
                        tax_rate_pct,
                        erp_is_gross=True,
                    )
                    price_row["listPrice"] = {
                        "currencyId": currency_id,
                        "gross": lp_gross,
                        "net": lp_net,
                        "linked": True,
                    }
                payload["price"] = [price_row]

                # Push the Shopware Tax UUID when we can resolve it
                # from the Sync's tax_template. Without this Shopware
                # falls back to the storefront's default tax which may
                # not match the rate we computed gross from.
                tax_id = _resolve_shopware_tax_id(tax_rate_pct)
                if tax_id:
                    payload["taxId"] = tax_id

    # Categories: link the product to its Item-Group's backend category
    # (written by Catalog Mirror on its own apply). When the mapping is
    # missing we skip silently here — the pre-flight check warns the
    # operator up-front, so reaching this branch with no mapping means
    # they consciously chose to push without categories.
    if _wants("categories"):
        category_ids = _resolve_shopware_category_ids(item)
        if category_ids:
            payload["categories"] = [{"id": cid} for cid in category_ids]

    # Images: deliberately NOT included in the sync-API payload.
    # Shopware's sync endpoint with ``media: [{media: {url}}]`` only
    # creates empty media records — it does NOT trigger the URL
    # download. The correct pattern is a separate two-step call
    # (``POST /_action/media/{id}/upload?fileName=&extension=`` with
    # ``{"url": ...}``) which the orchestrator handles after the
    # product upsert. See ``ShopwareProductAdapter.upload_product_images``
    # for the actual fetch path. Including the field here would create
    # empty media entities and trigger forever-drift on the next
    # preview.

    # Delivery time: pass the free-form ``Item.delivery_time`` string
    # ("10-15 Tage" etc.) through as ``deliveryTime``; the adapter
    # resolves it to a Shopware ``delivery_time`` uuid at apply-time
    # via ``get_or_create_delivery_time``, batched per session so the
    # per-item overhead is a cached dict lookup. Setting
    # ``deliveryTimeId`` on the product is what surfaces the
    # "Lieferzeit" badge on the PDP + listing.
    if _wants("basic"):
        delivery_time_str = (basic.get("delivery_time") or "").strip()
        if delivery_time_str:
            payload["deliveryTime"] = delivery_time_str

        # AI customFields: surface short description, benefits,
        # SEO meta description and youtube video URL via the
        # Shopware ``customFields`` JSON column. Storefront blocks
        # for "Benefits"/"Short Description"/SEO meta read these.
        # Hashed under ``basic`` (see ``_canonical_basic``) so a
        # Gemini re-run that rewrites them triggers drift through
        # the standard section-delta path. Only non-empty values
        # are emitted — Shopware merges into existing
        # ``customFields`` rather than replacing, so we never
        # accidentally clear an unrelated field set manually in
        # Admin UI.
        from ecommerce_integrations.shopware6.constants import (
            SHOPWARE_CUSTOM_FIELD_AI_BENEFITS,
            SHOPWARE_CUSTOM_FIELD_AI_SEO_DESCRIPTION,
            SHOPWARE_CUSTOM_FIELD_AI_SHORT_DESCRIPTION,
            SHOPWARE_CUSTOM_FIELD_YOUTUBE_VIDEO_URL,
        )
        cf_payload: dict[str, Any] = {}
        for canonical_key, shopware_key in (
            ("ai_short_description", SHOPWARE_CUSTOM_FIELD_AI_SHORT_DESCRIPTION),
            ("ai_benefits", SHOPWARE_CUSTOM_FIELD_AI_BENEFITS),
            ("ai_seo_description", SHOPWARE_CUSTOM_FIELD_AI_SEO_DESCRIPTION),
            ("youtube_video_url", SHOPWARE_CUSTOM_FIELD_YOUTUBE_VIDEO_URL),
        ):
            v = (basic.get(canonical_key) or "").strip()
            if v:
                cf_payload[shopware_key] = v
        if cf_payload:
            payload["customFields"] = cf_payload

    # Manufacturer (== ``Item.brand`` for our purposes — Shopware has
    # no separate "brand" concept; the manufacturer entity carries
    # that semantically). Nested upsert with a deterministic id keyed
    # off the manufacturer name: same input → same id → Shopware does
    # an UPDATE on the existing row instead of an INSERT-collision,
    # so re-running the sync doesn't spawn duplicate manufacturers
    # and we don't need an out-of-band "ensure manufacturer exists"
    # round-trip per item.
    if _wants("properties"):
        props_canonical = canonical.get("properties") or {}
        brand = props_canonical.get("brand")
        if brand:
            # Use the same deterministic UUID scheme as
            # ``shopware6.export.product_mapper.get_or_create_manufacturer``
            # (and now ``ensure_brand_entities_bulk``) so the m2m link
            # references the manufacturer row that's already enriched
            # with logo + description + link — not a parallel row
            # spawned by an older hash scheme.
            from ecommerce_integrations.shopware6.export.utils import (
                generate_uuid as _gen,
            )
            payload["manufacturer"] = {
                "id": _gen(f"manufacturer_{brand}"),
                "name": brand,
            }

        # Properties m2m — filterable properties from ``ecommerce_properties``.
        # The option entities must already exist server-side (the
        # apply pipeline runs ``ensure_property_options_bulk`` ahead
        # of the product bulk upsert); we just emit the m2m link as
        # nested ``properties: [{id: ...}]`` so Shopware writes the
        # association in the SAME ``_action/sync`` call as the
        # product itself. Cuts an entire round-trip per product.
        from ecommerce_integrations.product_sync.engine.adapters.shopware import (
            property_option_uuid,
        )
        ecom_props = props_canonical.get("ecommerce_properties") or []
        if ecom_props:
            payload["properties"] = [
                {"id": property_option_uuid(p["name"], p["value"], kind="property")}
                for p in ecom_props
                if p.get("name") and p.get("value")
            ]

        # Variant options m2m — for variants only. The attribute set
        # comes from ``canonical.properties.attributes`` (sourced
        # from the Item.attributes child table). Emit as nested
        # ``options: [{id: ...}]`` so Shopware writes the m2m link in
        # the same call instead of a follow-up PATCH per variant.
        if getattr(item, "variant_of", None):
            attrs = props_canonical.get("attributes") or []
            if attrs:
                payload["options"] = [
                    {"id": property_option_uuid(a["name"], a["value"], kind="variant")}
                    for a in attrs
                    if a.get("name") and a.get("value")
                ]

    # Variant link: when this Item is a variant of a template
    # (``Item.variant_of`` set), Shopware needs the template's
    # external_id as ``parentId`` so the variant shows up under its
    # template instead of as a standalone product (the user-reported
    # symptom: PARENT-suffixed templates appearing as orphan landing
    # pages on the storefront because their children weren't linked
    # back). Skip silently when the template hasn't been pushed yet
    # — the next sync round will pick it up once the template lands
    # an external_id.
    variant_of = getattr(item, "variant_of", None) or ""
    if variant_of:
        parent_ext = frappe.db.get_value(
            "Ecommerce Item",
            {"erpnext_item_code": variant_of, "integration": "shopware6"},
            "integration_item_code",
        )
        if parent_ext:
            payload["parentId"] = parent_ext

    # TODO Phase-5.1: per-channel prices via Shopware Rule engine
    # TODO Phase-5.1: properties (attributes — Shopware property groups)
    return payload


def build_medusa_payload(
    item, sync, canonical: dict[str, Any], *,
    external_id: str | None = None,
    variant_id: str | None = None,
) -> dict[str, Any]:
    """Medusa v2 product payload.

    Per the Medusa v2 research: only ``title`` is strictly required;
    everything else is optional. We set ``external_id`` to the ERPNext
    item_code so subsequent upserts can find the row by lookup. POST
    semantics (not PATCH) mean nested arrays REPLACE — variants and
    images are sent as full state.

    ``variant_id``: stored on ``tabEcommerce Item.variant_id``. When the
    Medusa product carries MULTIPLE variants (each ERPNext item maps to
    one of them via its own ``variant_id``), passing the id here makes
    Medusa patch that specific variant instead of trying to ADD a new
    one — which would otherwise fail with
    ``"Product variant with sku: X, already exists."`` and abort every
    item that shares the product.
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
    # Patch-vs-create: include the variant id when we have one. Without
    # it, Medusa interprets the entry as "add a new variant" and rejects
    # with a SKU-conflict 400 when an existing variant already holds the
    # same SKU (which is always the case for an UPDATE on a previously
    # synced item).
    if variant_id:
        variant["id"] = variant_id
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


# ─── Tax + image helpers (Shopware-specific) ─────────────────────────


def _resolve_tax_rate_pct(sync, item=None) -> float:
    """Tax rate (%) the price computation should use.

    Resolution order — narrowest-scope wins, exactly the same order
    ERPNext itself uses on Sales Invoices:

    1. ``Item.taxes[].item_tax_template`` — per-item override.
       Catches mixed-rate catalogues (e.g. books at 7 % alongside
       other goods at 19 %).
    2. ``Sync.tax_template`` — operator-set default for the Sync.
    3. ``Shopware Setting.default_tax_rate`` — legacy fallback for
       installs that still carry the column.
    4. ``19.0`` — German standard rate.

    The "rate" of a template is the highest non-zero ``tax_rate``
    across its child rows — German-style charts of accounts have
    many zero rows (Umsatzsteuer + Vorsteuer + innergem. Erwerb …)
    and a handful at the actual sales-tax rate. Picking the max
    picks the sales-tax rate without having to enumerate per-country
    accounting conventions.

    Returns a percentage (19.0, not 0.19). Never raises.
    """
    # Lazy import — the canonical module's helpers are the source of
    # truth so the two callsites can't drift.
    from ecommerce_integrations.product_sync.engine.canonical import (
        _max_tax_rate_from_item,
        _max_tax_rate_from_template,
    )
    if item is not None:
        rate = _max_tax_rate_from_item(item)
        if rate:
            return rate

    rate = _max_tax_rate_from_template(
        (getattr(sync, "tax_template", "") or "").strip(),
    )
    if rate:
        return rate

    # Setting-level legacy fallback.
    try:
        setting = frappe.get_single("Shopware Setting")
        legacy = getattr(setting, "default_tax_rate", None)
        if legacy is not None and float(legacy) > 0:
            return float(legacy)
    except Exception:  # noqa: BLE001
        pass

    return 19.0



def _compute_gross_net(
    base_price: float, tax_rate_pct: float, *, erp_is_gross: bool,
) -> tuple[float, float]:
    """Return ``(gross, net)`` rounded to 4 decimals.

    Shopware wants both numbers explicitly. The tax_rate_pct decides
    the conversion factor; ``erp_is_gross`` decides which side ERPNext
    already has.
    """
    factor = 1.0 + (tax_rate_pct / 100.0)
    if erp_is_gross:
        gross = base_price
        net = base_price / factor if factor else base_price
    else:
        net = base_price
        gross = base_price * factor
    return round(gross, 4), round(net, 4)


def _resolve_shopware_tax_id(tax_rate_pct: float) -> str | None:
    """Look up the Shopware ``tax.id`` UUID for a given tax rate.

    Shopware ships a Tax entity per rate ("19 %", "7 %", …). The
    product references one of them. We cache the rate→id map for an
    hour so this is a single cold lookup per sync run, not per item.

    Returns ``None`` on lookup failure — caller leaves ``taxId`` off
    the payload and Shopware uses the storefront's default tax.
    """
    if tax_rate_pct is None or tax_rate_pct <= 0:
        return None
    cache_key = f"_psync_shopware_tax_id:{tax_rate_pct:.2f}"
    cached = frappe.cache().get_value(cache_key)
    if cached:
        return cached or None
    try:
        from ecommerce_integrations.shopware6.connection import temp_shopware_session

        @temp_shopware_session
        def _lookup(client):
            resp = client.request_post(
                "search/tax",
                payload={
                    "limit": 1,
                    "filter": [{
                        "type": "equals",
                        "field": "taxRate",
                        "value": tax_rate_pct,
                    }],
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
    except Exception:  # noqa: BLE001
        return None


def _resolve_image_public_base() -> str:
    """Resolve the public URL base used to absolutize image paths.

    Memoised on ``frappe.local`` for the duration of the current
    request/job — ``frappe.get_single`` re-loads child tables every
    call (no built-in cache), and on a 37k-item apply that adds up
    to hours of pure DB I/O just to read one varchar.
    """
    cached = getattr(getattr(frappe, "local", None), "_shopware_image_base", None)
    if cached is not None:
        return cached
    base = ""
    try:
        base = (
            frappe.db.get_single_value("Shopware Setting", "image_public_base_url")
            or ""
        ).strip()
    except Exception:  # noqa: BLE001
        pass
    if not base:
        try:
            base = (frappe.utils.get_url() or "").strip()
        except Exception:  # noqa: BLE001
            base = ""
    base = base.rstrip("/")
    try:
        frappe.local._shopware_image_base = base  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass
    return base


def _build_shopware_media(images: list[dict]) -> list[dict]:
    """Translate canonical's image list into Shopware ``media`` format.

    Each entry becomes ``{"media": {"url": <absolute>, "filename": …}}``
    so Shopware fetches the binary from the supplied URL on its side.

    URL resolution:

    1. ``Shopware Setting.image_public_base_url`` — operator-set public
       base (e.g. CDN or reverse-proxy URL). When set, all relative
       URLs are prefixed here.
    2. ``frappe.utils.get_url()`` — the site's own base URL. Only
       useful when Shopware can reach the ERP host.
    3. If the canonical URL is already absolute, pass through unchanged.

    When none of the above produces a URL Shopware could reach, the
    function returns ``[]`` — callers omit the ``media`` field entirely
    so Shopware keeps existing media (POST semantics treat missing
    arrays as "leave alone").
    """
    if not images:
        return []
    public_base = _resolve_image_public_base()

    out: list[dict] = []
    for img in images:
        raw_url = (img.get("url") or "").strip()
        if not raw_url:
            continue
        if raw_url.startswith(("http://", "https://")):
            absolute = raw_url
        elif public_base:
            absolute = public_base + (raw_url if raw_url.startswith("/") else "/" + raw_url)
        else:
            # No way to resolve — skip rather than push a broken ref.
            continue
        # Filename hint helps Shopware name the imported media nicely.
        filename = absolute.rsplit("/", 1)[-1] or "image"
        out.append({
            "media": {
                "url": absolute,
                "filename": filename,
            },
        })
    return out
