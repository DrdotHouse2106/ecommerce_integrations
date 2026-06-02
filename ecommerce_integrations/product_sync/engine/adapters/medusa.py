"""Medusa ProductAdapter — Product Sync ↔ Medusa v2 product catalogue.

Wraps the Medusa Admin API in the contract laid out by
:class:`ProductAdapter`. Mirrors the structural choices made in
:mod:`product_sync.engine.adapters.shopware`, but the wire format is
quite different and a few quirks need to be respected to keep things
idempotent:

1. **POST, not PATCH.** Medusa v2 only exposes ``POST /admin/products``
   for both create and update. There is no merge-patch semantics — any
   array field in the body (``variants``, ``images``, ``options``,
   ``sales_channels`` …) replaces the existing array wholesale on
   update. Callers that only intend to touch a scalar must still send
   the arrays they want to keep, otherwise the unmentioned arrays
   silently empty out.
2. **``external_id`` is the operator-controlled identity column.**
   Medusa keeps its own opaque ``id`` (``prod_…``) but we drive
   lookups through ``external_id`` so the ERP-side ``item_code`` is
   the source of truth. Lookups are done with
   ``GET /admin/products?external_id={id}``; the response gives back
   the Medusa-internal ``id`` we then use to POST to.
3. **No "active" flag.** ``status=draft`` is the standard way to hide
   a product from the storefront; that's what :meth:`deactivate_product`
   sets. Hard delete uses ``DELETE /admin/products/{id}``; Medusa
   soft-deletes (``deleted_at``) but the product is no longer visible
   from the admin endpoints.
4. **Prices are integers in minor units (cents).** All money values
   land on the wire as ``amount=1234`` for €12.34. We convert at the
   adapter boundary so the engine-level canonical payload speaks in
   floats just like the Shopware side.

Live-side hashing (:meth:`MedusaProductAdapter.compute_live_hash`)
emits the same canonical sections as
:func:`product_sync.engine.canonical.build_canonical_payload`. The
section keys must stay in lock-step with the ERP-side builders or the
differ will flap (live-hash says one thing, ERP-hash says another,
push, still don't match, push again…).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from ecommerce_integrations.medusa.connection import (
    get_medusa_session,
    medusa_request,
)
from ecommerce_integrations.product_sync.constants import BACKEND_MEDUSA
from ecommerce_integrations.product_sync.engine.adapters.base import (
    AdapterError,
    LiveProductNode,
    ProductAdapter,
    ProductMatch,
)
from ecommerce_integrations.product_sync.engine.canonical import (
    PAYLOAD_VERSION,
    compute_hash,
)

# Medusa v2 paginates with offset/limit. 200 is a comfortable page
# size — the admin endpoint accepts more, but throughput plateaus
# around here and bigger pages just inflate per-request latency on a
# remote Medusa instance.
_FETCH_PAGE_SIZE = 200

# Field projection for ``GET /admin/products``. Without an explicit
# ``fields`` argument Medusa returns a slim product shape that omits
# the variant-level inventory + price data we need for the live hash.
# Keep this in sync with the read paths in ``_node_from_product`` and
# ``_build_live_canonical`` — drop a field here and the live-hash
# silently goes stale.
_FETCH_FIELDS = (
    "id,title,description,status,metadata,variants.*,variants.prices.*,"
    "sales_channels.*,categories.*,thumbnail,images.*"
)

# Match-workflow result cap. The Match UI shows the top handful;
# anything more is just noise.
_MATCH_LIMIT = 10


class MedusaProductAdapter(ProductAdapter):
    """Concrete adapter against the Medusa v2 Admin API.

    Every public method opens its own short-lived
    ``requests.Session`` via :func:`get_medusa_session` and closes it
    in a ``finally`` so we don't leak sockets when a call raises.
    HTTP-level retries on overload happen inside
    :func:`medusa_request` itself, so adapter code only has to handle
    final failure.
    """

    backend = BACKEND_MEDUSA

    # ─── Public adapter surface ──────────────────────────────────────

    def fetch_products(
        self,
        *,
        sales_channel_ids: list[str] | None = None,
        category_ids: list[str] | None = None,
        external_ids: list[str] | None = None,
    ) -> Iterator[LiveProductNode]:
        # Iterator pattern: yield per-page. Materialising a five-figure
        # catalogue in one list would peak memory and defeat the point
        # of streaming.
        session, base_url = get_medusa_session()
        try:
            yield from self._iter_products(
                session,
                base_url,
                sales_channel_ids=sales_channel_ids,
                category_ids=category_ids,
                external_ids=external_ids,
            )
        finally:
            session.close()

    def upsert_product(
        self,
        *,
        external_id: str | None,
        payload: dict,
        target_sales_channels: list[str],
    ) -> str:
        session, base_url = get_medusa_session()
        try:
            return self._upsert_impl(
                session,
                base_url,
                external_id=external_id,
                payload=payload,
                target_sales_channels=target_sales_channels,
            )
        finally:
            session.close()

    def upsert_products_bulk(
        self,
        items: list[dict],
        *,
        target_sales_channels: list[str],
    ) -> list[dict]:
        """Bulk-upsert via the optional ``erpnext-sync`` Medusa plugin.

        When the plugin is installed on the Medusa side it exposes
        ``POST /admin/erpnext-sync/products`` which accepts a flat
        list of product DTOs and handles create+update in one
        ``batchProductsWorkflow`` call. Cuts the per-batch HTTP
        round-trips from ~N (one per item) to 1 — orders of
        magnitude faster on five-figure catalogues since Medusa v2
        has no native batch product endpoint exposed on the Admin
        REST API.

        Detection is a one-time ``OPTIONS`` probe cached on
        ``frappe.local`` for the lifetime of the job. Missing plugin
        falls through to the per-item base-class implementation so
        the adapter keeps working on installs without the plugin.
        Per-batch transient failures (validation, server error) also
        fall through to per-item so a single bad payload doesn't
        kill the whole batch — the per-item path surfaces the
        specific failure to the orchestrator's audit row.
        """
        if not items:
            return []
        session, base_url = get_medusa_session()
        try:
            if not self._erpnext_sync_plugin_available(session, base_url):
                return super().upsert_products_bulk(
                    items, target_sales_channels=target_sales_channels,
                )
            return self._bulk_upsert_via_plugin(
                session, base_url, items, target_sales_channels,
            )
        finally:
            session.close()

    def _erpnext_sync_plugin_available(self, session, base_url: str) -> bool:
        """Cached OPTIONS probe — does the bulk plugin live here?

        ``OPTIONS`` is the lightest probe: present routes respond
        with a non-5xx status (200/204/405 depending on framework
        config), missing routes 404. We treat anything in 2xx/4xx
        EXCEPT 404 as "endpoint exists" — auth (401/403), validation
        (400), method-not-allowed (405) all mean the route is wired
        up. The probe is keyed per ``base_url`` so multi-Medusa
        installs (sandbox / prod) don't share verdicts.
        """
        import frappe
        cache_key = f"_psync_erpnext_sync_plugin::{base_url}"
        cached = getattr(frappe.local, cache_key, None)
        if cached is not None:
            return cached
        try:
            r = session.request(
                "OPTIONS",
                f"{base_url}/admin/erpnext-sync/products",
                timeout=(5, 5),
            )
            available = r.status_code != 404
        except Exception:  # noqa: BLE001
            # Network glitch — assume unavailable so we fall back to
            # per-item rather than blocking the whole sync on a
            # transient probe failure. Cache the False so the rest
            # of the job uses the per-item path consistently.
            available = False
        setattr(frappe.local, cache_key, available)
        return available

    def _bulk_upsert_via_plugin(
        self,
        session,
        base_url: str,
        items: list[dict],
        target_sales_channels: list[str],
    ) -> list[dict]:
        """Call ``POST /admin/erpnext-sync/products`` with the
        operator-plugin's DTO shape and fan the aggregate response
        back into the per-item result list the orchestrator expects.

        The plugin returns ``{success, created, updated}`` without
        per-item ids, so on success every input row gets a
        no-error result; on failure every row gets the batch-level
        error message (matching the orchestrator's existing
        whole-batch failure semantics in ``_apply_batch``)."""
        products = [
            self._to_plugin_dto(entry, target_sales_channels)
            for entry in items
        ]
        # Map each input entry to the external_id we actually sent —
        # for CREATE the orchestrator passed ``external_id=None`` and
        # the DTO transformer derived it from ``payload.external_id``
        # (= ERPNext item_code). The stored mapping needs that value,
        # not ``None``, otherwise the next sync sees a missing
        # mapping and tries to CREATE again. Re-deriving here keeps
        # the bulk path's stored ids consistent with the per-item
        # fallback's behaviour.
        sent_ids = [
            (e.get("external_id") or (e.get("payload") or {}).get("external_id"))
            for e in items
        ]
        try:
            medusa_request(
                session, base_url, "POST",
                "/admin/erpnext-sync/products",
                json={"products": products},
            )
        except Exception as exc:  # noqa: BLE001
            err = str(exc)
            return [{"external_id": sid, "error": err} for sid in sent_ids]
        return [{"external_id": sid, "error": None} for sid in sent_ids]

    def _to_plugin_dto(
        self,
        entry: dict,
        target_sales_channels: list[str],
    ) -> dict:
        """Flatten the engine's payload into the plugin's DTO.

        Engine payload is Medusa-product-native (``variants[]`` with
        sku/prices, ``sales_channels[]`` as objects); plugin DTO is
        flat (single sku/prices at product level, id-only
        ``sales_channel_ids``). Multi-variant items are out of scope
        for the bulk path — the plugin's product=variant mapping
        suffices for the simple-product catalogue this engine
        already targets.
        """
        # On CREATE the orchestrator passes ``external_id=None`` since
        # no mapping exists yet — but the plugin's Zod contract
        # ``products[].external_id`` requires a non-empty string (the
        # operator-side identifier, our ERPNext item_code). The
        # payload built by :func:`build_medusa_payload` always carries
        # ``external_id = item.item_code`` at the top level, so prefer
        # that when the orchestrator entry doesn't supply one.
        p = entry.get("payload") or {}
        ext_id = entry.get("external_id") or p.get("external_id")
        first_variant = (p.get("variants") or [{}])[0] if p.get("variants") else {}
        dto: dict = {
            "external_id": ext_id,
            "title": p.get("title") or ext_id,
        }
        sku = first_variant.get("sku") or p.get("sku") or ext_id
        if sku:
            dto["sku"] = sku
        for k in ("description", "status", "subtitle", "thumbnail", "weight"):
            v = p.get(k)
            if v is not None:
                dto[k] = v
        prices = first_variant.get("prices") or []
        if prices:
            dto["prices"] = prices
        # Channel ids: prefer the per-item canonical list (from
        # ``visibilities`` section), broadcast targets only when the
        # per-item path didn't set anything. Same precedence as
        # ``_upsert_impl``.
        sc_objs = p.get("sales_channels") or []
        if sc_objs:
            sc_ids = [s.get("id") for s in sc_objs if isinstance(s, dict) and s.get("id")]
        else:
            sc_ids = [s for s in (target_sales_channels or []) if s]
        if sc_ids:
            dto["sales_channel_ids"] = sc_ids
        cat_objs = p.get("categories") or []
        cat_ids = [c.get("id") for c in cat_objs if isinstance(c, dict) and c.get("id")]
        if cat_ids:
            dto["category_ids"] = cat_ids
        meta = p.get("metadata")
        if meta:
            dto["metadata"] = meta
        return dto

    def deactivate_product(self, external_id: str) -> None:
        if not external_id:
            return
        session, base_url = get_medusa_session()
        try:
            product_id = self._resolve_product_id(
                session, base_url, external_id,
            )
            if not product_id:
                # Idempotent: nothing to deactivate.
                return
            try:
                medusa_request(
                    session,
                    base_url,
                    "POST",
                    f"/admin/products/{product_id}",
                    json={"status": "draft"},
                )
            except Exception as e:
                raise AdapterError(
                    f"Medusa product deactivate failed ({external_id}): {e}",
                ) from e
        finally:
            session.close()

    def delete_product(self, external_id: str) -> None:
        if not external_id:
            return
        session, base_url = get_medusa_session()
        try:
            product_id = self._resolve_product_id(
                session, base_url, external_id,
            )
            if not product_id:
                # Idempotent: 404-equivalent. Treat as already deleted.
                return
            try:
                medusa_request(
                    session,
                    base_url,
                    "DELETE",
                    f"/admin/products/{product_id}",
                )
            except Exception as e:
                # 404 = already gone; idempotent per the ABC contract.
                if "404" in str(e):
                    return
                raise AdapterError(
                    f"Medusa product delete failed ({external_id}): {e}",
                ) from e
        finally:
            session.close()

    def find_matching_products(
        self,
        *,
        sku: str | None = None,
        name: str | None = None,
        ean: str | None = None,
    ) -> list[ProductMatch]:
        # Note: the ABC only declares ``sku``/``name``; ``ean`` is an
        # extra keyword the Medusa adapter accepts because Medusa
        # carries EAN on the variant. Keyword-only + default ``None``
        # keeps the override covariant with the base.
        if not (sku or name or ean):
            return []

        session, base_url = get_medusa_session()
        try:
            return self._find_matching_impl(
                session, base_url, sku=sku, name=name, ean=ean,
            )
        finally:
            session.close()

    # ─── Hashing parity with engine/canonical.py ─────────────────────

    def compute_live_hash(
        self, product: dict[str, Any], sync,
    ) -> str:
        """Hash a live Medusa product the same way the ERP side hashes
        the source Item, so the differ can compare apples to apples.

        ``product`` is the raw response dict from the
        ``products[]`` array of ``GET /admin/products``. ``sync`` is
        the ``Ecommerce Product Sync`` doc; toggles on the sync gate
        which sections land in the payload, matching the ERP-side
        :func:`build_canonical_payload`.
        """
        payload = self._build_live_canonical(product, sync)
        return compute_hash(payload)

    # ─── Implementations ─────────────────────────────────────────────

    def _iter_products(
        self,
        session,
        base_url: str,
        *,
        sales_channel_ids: list[str] | None,
        category_ids: list[str] | None,
        external_ids: list[str] | None,
    ) -> Iterator[LiveProductNode]:
        native_ids: list[str] = []
        legacy_external_ids = external_ids
        if external_ids:
            native_ids = [v for v in external_ids if str(v or "").startswith("prod_")]
            legacy_external_ids = [v for v in external_ids if v not in native_ids]

        for product_id in native_ids:
            try:
                resp = medusa_request(
                    session,
                    base_url,
                    "GET",
                    f"/admin/products/{product_id}",
                    params={"fields": _FETCH_FIELDS},
                )
            except Exception as e:
                if "404" in str(e):
                    continue
                raise AdapterError(
                    f"Medusa product fetch failed (id={product_id!r}): {e}",
                ) from e
            product = (resp or {}).get("product") or {}
            node = self._node_from_product(product)
            if node is not None and _matches_filters(
                node,
                sales_channel_ids=sales_channel_ids,
                category_ids=category_ids,
            ):
                yield node

        if external_ids and not legacy_external_ids:
            return

        params = self._build_fetch_params(
            sales_channel_ids=sales_channel_ids,
            category_ids=category_ids,
            external_ids=legacy_external_ids,
        )
        offset = 0
        while True:
            page_params = dict(params)
            page_params["limit"] = _FETCH_PAGE_SIZE
            page_params["offset"] = offset

            try:
                resp = medusa_request(
                    session,
                    base_url,
                    "GET",
                    "/admin/products",
                    params=page_params,
                )
            except Exception as e:
                raise AdapterError(
                    f"Medusa product list failed (offset={offset}): {e}",
                ) from e

            products = (resp or {}).get("products") or []
            if not products:
                return
            for p in products:
                node = self._node_from_product(p)
                if node is not None:
                    yield node
            # Same termination guard as the catalog adapter — short
            # page means we've drained the result set.
            if len(products) < _FETCH_PAGE_SIZE:
                return
            offset += _FETCH_PAGE_SIZE

    def _build_fetch_params(
        self,
        *,
        sales_channel_ids: list[str] | None,
        category_ids: list[str] | None,
        external_ids: list[str] | None,
    ) -> dict[str, Any]:
        """Compose the query-param dict for ``/admin/products``.

        Medusa accepts repeated query keys for list-valued filters.
        Product Sync stores Medusa's internal ``prod_...`` id in
        ``tabEcommerce Item.integration_item_code``; legacy rows may
        still contain an operator ``external_id``. Use the native id
        filter for normal rows and keep the external-id filter as a
        compatibility fallback.
        """
        params: dict[str, Any] = {"fields": _FETCH_FIELDS}
        if external_ids:
            native_ids = [v for v in external_ids if str(v or "").startswith("prod_")]
            legacy_external_ids = [v for v in external_ids if v not in native_ids]
            if native_ids:
                params["id[]"] = list(native_ids)
            if legacy_external_ids:
                params["external_id[]"] = list(legacy_external_ids)
        if sales_channel_ids:
            params["sales_channel_id[]"] = list(sales_channel_ids)
        if category_ids:
            params["category_id[]"] = list(category_ids)
        return params

    def _node_from_product(
        self, product: dict[str, Any],
    ) -> LiveProductNode | None:
        ext_id = product.get("id")
        if not ext_id:
            return None

        variants = product.get("variants") or []
        first_variant: dict[str, Any] = (
            variants[0] if variants and isinstance(variants[0], dict) else {}
        )

        sku = first_variant.get("sku") or None

        price_val: float | None = None
        prices = first_variant.get("prices") or []
        if isinstance(prices, list) and prices:
            first_price = prices[0] or {}
            amount = first_price.get("amount")
            try:
                if amount is not None:
                    # Medusa stores prices in minor units (cents). Engine-
                    # side hashing speaks majors, so we divide here.
                    price_val = float(amount) / 100.0
            except (TypeError, ValueError):
                price_val = None

        stock_raw = first_variant.get("inventory_quantity")
        try:
            stock_val: int | None = (
                int(stock_raw) if stock_raw is not None else 0
            )
        except (TypeError, ValueError):
            stock_val = 0

        cat_ids: list[str] = []
        for c in product.get("categories") or []:
            if not isinstance(c, dict):
                continue
            cid = c.get("id")
            if cid:
                cat_ids.append(cid)
        cat_ids = sorted(set(cat_ids))

        sc_ids: list[str] = []
        for s in product.get("sales_channels") or []:
            if not isinstance(s, dict):
                continue
            sid = s.get("id")
            if sid:
                sc_ids.append(sid)
        sc_ids = sorted(set(sc_ids))

        images: list[str] = []
        for img in product.get("images") or []:
            if not isinstance(img, dict):
                continue
            url = img.get("url")
            if url:
                images.append(url)
        images = sorted(set(images))

        return LiveProductNode(
            external_id=ext_id,
            sku=sku,
            name=product.get("title") or "",
            description=product.get("description") or None,
            price=price_val,
            stock=stock_val,
            category_ids=cat_ids,
            sales_channel_ids=sc_ids,
            active=(product.get("status") == "published"),
            images=images,
        )

    def _upsert_impl(
        self,
        session,
        base_url: str,
        *,
        external_id: str | None,
        payload: dict,
        target_sales_channels: list[str],
    ) -> str:
        out_payload: dict[str, Any] = dict(payload or {})

        # Sales-channel assignment is opinionated by Product Sync. If
        # the caller supplied targets AND the payload doesn't already
        # carry per-item ``sales_channels`` (= the new visibility
        # path, populated by ``build_medusa_payload`` from the
        # canonical's ``visibilities`` section when
        # ``sync_visibilities=1``), broadcast the sync's targets to
        # every item. The per-item path wins when present so the
        # broadcast doesn't overwrite per-item Smart-Collection /
        # Catalog-Mirror decisions. Empty list means "no opinion" —
        # leave whatever Medusa already has, because clobbering with
        # ``[]`` would unlink every channel on update.
        if target_sales_channels and "sales_channels" not in out_payload:
            out_payload["sales_channels"] = [
                {"id": sc_id} for sc_id in target_sales_channels if sc_id
            ]

        product_id: str | None = None
        if external_id:
            # Fast path: the stored mapping already holds Medusa's
            # native ``prod_*`` id. Skip the existence-check GET that
            # ``_resolve_product_id`` would otherwise do and PATCH
            # the product directly — if it's gone (404) the fallback
            # below recreates it. Saves one round trip per item on
            # the update path (= ~50 % of sync wall-time at 5 k+ items
            # since Medusa has no bulk product endpoint).
            if str(external_id).startswith("prod_"):
                product_id = external_id
            else:
                product_id = self._resolve_product_id(
                    session, base_url, external_id,
                )
            # Always keep the operator-supplied external_id on the
            # body so Medusa re-adopts the same identity if we end
            # up creating (404 fallback or fresh mapping).
            out_payload.setdefault("external_id", external_id)

        def _do_create() -> dict[str, Any]:
            create_payload = dict(out_payload)
            create_payload.pop("id", None)
            return medusa_request(
                session, base_url, "POST",
                "/admin/products", json=create_payload,
            )

        try:
            if product_id:
                try:
                    resp = medusa_request(
                        session, base_url, "POST",
                        f"/admin/products/{product_id}",
                        json=out_payload,
                    )
                except Exception as e:
                    # Stored id stale (product deleted server-side) —
                    # fall back to create so the operator's intent
                    # ("ensure this item exists in Medusa") is honoured
                    # even after an out-of-band deletion. Caller's
                    # downstream code rewrites the mapping with the
                    # new id from the response.
                    if "404" in str(e):
                        resp = _do_create()
                    else:
                        raise
            else:
                resp = _do_create()
        except Exception as e:
            raise AdapterError(
                f"Medusa product upsert failed "
                f"(external_id={external_id!r}): {e}",
            ) from e

        prod = (resp or {}).get("product") or {}
        new_id = prod.get("id") or product_id
        if not new_id:
            raise AdapterError(
                f"Medusa product upsert returned no id "
                f"(external_id={external_id!r}, response={resp!r})",
            )
        return new_id

    def _resolve_product_id(
        self, session, base_url: str, external_id: str,
    ) -> str | None:
        """Look up the Medusa-internal ``id`` for a stored mapping.

        Current mappings store Medusa's internal ``prod_...`` id. Older
        rows may still store an operator ``external_id``; those are
        resolved via list lookup below. Returns ``None`` when no product
        exists for the stored value.
        """
        if str(external_id or "").startswith("prod_"):
            try:
                resp = medusa_request(
                    session,
                    base_url,
                    "GET",
                    f"/admin/products/{external_id}",
                    params={"fields": "id"},
                )
            except Exception as e:
                if "404" in str(e):
                    return None
                raise AdapterError(
                    f"Medusa product lookup failed "
                    f"(id={external_id!r}): {e}",
                ) from e
            product = (resp or {}).get("product") or {}
            return product.get("id") or None

        try:
            resp = medusa_request(
                session,
                base_url,
                "GET",
                "/admin/products",
                params={
                    "external_id[]": [external_id],
                    "limit": 1,
                    "fields": "id,external_id",
                },
            )
        except Exception as e:
            raise AdapterError(
                f"Medusa product lookup failed "
                f"(external_id={external_id!r}): {e}",
            ) from e

        products = (resp or {}).get("products") or []
        if not products:
            return None
        first = products[0] or {}
        return first.get("id")

    def _find_matching_impl(
        self,
        session,
        base_url: str,
        *,
        sku: str | None,
        name: str | None,
        ean: str | None,
    ) -> list[ProductMatch]:
        matches: list[ProductMatch] = []
        seen: set[str] = set()

        # SKU is the strongest signal: 1.0. Variant-level filter — a
        # product with a matching variant SKU is treated as the
        # candidate.
        if sku:
            for m in self._run_match_query(
                session,
                base_url,
                params={
                    "variants[sku]": sku,
                    "limit": _MATCH_LIMIT,
                    "fields": "id,title,variants.sku",
                },
                score=1.0,
            ):
                if m.external_id not in seen:
                    seen.add(m.external_id)
                    matches.append(m)

        # EAN second: 0.8. Different products legitimately share an
        # EAN (re-issued barcodes, multi-pack vs single).
        if ean:
            for m in self._run_match_query(
                session,
                base_url,
                params={
                    "variants[ean]": ean,
                    "limit": _MATCH_LIMIT,
                    "fields": "id,title,variants.sku",
                },
                score=0.8,
            ):
                if m.external_id not in seen:
                    seen.add(m.external_id)
                    matches.append(m)

        # Name last: 0.5. Medusa's ``q`` parameter is a free-text
        # search; we cap at the first page of results and let the UI
        # rank below SKU/EAN hits.
        if name:
            for m in self._run_match_query(
                session,
                base_url,
                params={
                    "q": name,
                    "limit": _MATCH_LIMIT,
                    "fields": "id,title,variants.sku",
                },
                score=0.5,
            ):
                if m.external_id not in seen:
                    seen.add(m.external_id)
                    matches.append(m)

        matches.sort(key=lambda mm: mm.score, reverse=True)
        return matches[:_MATCH_LIMIT]

    def _run_match_query(
        self,
        session,
        base_url: str,
        *,
        params: dict[str, Any],
        score: float,
    ) -> list[ProductMatch]:
        try:
            resp = medusa_request(
                session,
                base_url,
                "GET",
                "/admin/products",
                params=params,
            )
        except Exception as e:
            raise AdapterError(
                f"Medusa product match search failed "
                f"(params={params!r}): {e}",
            ) from e

        out: list[ProductMatch] = []
        for prod in (resp or {}).get("products") or []:
            if not isinstance(prod, dict):
                continue
            ext_id = prod.get("id")
            if not ext_id:
                continue
            variants = prod.get("variants") or []
            first_variant = (
                variants[0]
                if variants and isinstance(variants[0], dict)
                else {}
            )
            out.append(
                ProductMatch(
                    external_id=ext_id,
                    sku=first_variant.get("sku") or None,
                    name=prod.get("title") or "",
                    score=score,
                ),
            )
        return out

    # ─── Live-hash payload builders ──────────────────────────────────

    def _build_live_canonical(
        self, product: dict[str, Any], sync,
    ) -> dict[str, Any]:
        """Build a canonical-payload dict from a live Medusa product.

        Section names and shapes mirror
        :func:`product_sync.engine.canonical.build_canonical_payload`
        exactly. Toggles are honoured via ``_live_flag`` — the same
        ``sync_*`` flags that gate the ERP-side sections gate the
        live-side ones, otherwise the differ would compare a
        toggle-trimmed ERP payload against a full live payload and
        always declare drift.
        """
        variants = product.get("variants") or []
        first_variant: dict[str, Any] = (
            variants[0] if variants and isinstance(variants[0], dict) else {}
        )
        metadata = product.get("metadata") or {}
        if not isinstance(metadata, dict):
            metadata = {}

        out: dict[str, Any] = {
            "v": PAYLOAD_VERSION,
            "item_code": _ns(first_variant.get("sku")),
            # Medusa variants live inside a product; the engine-level
            # canonical treats variants the same as standalone items
            # for hash purposes (variant-of mapping is a Phase-5
            # extension).
            "is_variant": False,
            "variant_of": None,
        }

        if _live_flag(sync, "sync_basic_fields", default=True):
            out["basic"] = self._live_basic(product, first_variant)
        if _live_flag(sync, "sync_pricing", default=True):
            out["pricing"] = self._live_pricing(first_variant)
        if _live_flag(sync, "sync_inventory", default=True):
            out["inventory"] = self._live_inventory(first_variant)
        if _live_flag(sync, "sync_images", default=True):
            out["images"] = self._live_images(product)
        if _live_flag(sync, "sync_properties", default=True):
            out["properties"] = self._live_properties(metadata)
        if _live_flag(sync, "sync_seo_fields", default=False):
            out["seo"] = self._live_seo(product, metadata)
        if _live_flag(sync, "sync_taxes", default=True):
            out["taxes"] = self._live_taxes()
        out["categories"] = self._live_categories(metadata)
        # Per-item visibilities — only emitted when the Sync opted
        # in via ``sync_visibilities=1`` (matches the ERP-side gate
        # in ``build_canonical_payload``). Medusa doesn't carry a
        # per-channel visibility level (binary in/out), so each
        # entry gets ``visibility = 30`` to stay symmetric with the
        # Shopware-side canonical.
        if _live_flag(sync, "sync_visibilities", default=False):
            out["visibilities"] = self._live_visibilities(product)
        return out

    def _live_visibilities(
        self, product: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Convert Medusa's ``product.sales_channels`` association
        to the engine-canonical visibility shape."""
        out: list[dict[str, Any]] = []
        for sc in (product.get("sales_channels") or []):
            if not isinstance(sc, dict):
                continue
            sc_id = sc.get("id")
            if not sc_id:
                continue
            out.append({
                "channel_id": _ns(sc_id),
                "visibility": 30,
            })
        out.sort(key=lambda d: d["channel_id"])
        return out

    def _live_basic(
        self,
        product: dict[str, Any],
        first_variant: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "name": _ns(product.get("title")),
            "sku": _ns(first_variant.get("sku")),
            "description": _ns(product.get("description")),
            "ean": _ns(first_variant.get("ean")),
            "is_active": (product.get("status") == "published"),
            # Medusa doesn't carry an item-level UoM; the engine-side
            # canonical hashes ``stock_uom`` (free-text) but there's
            # no equivalent live field. Emit empty string so the
            # presence-or-absence check stays deterministic; Phase 5
            # can map this onto a metadata key if operators need it.
            # TODO: surface UoM via metadata when Phase 5 lands.
            "uom": "",
        }

    def _live_pricing(
        self, first_variant: dict[str, Any],
    ) -> dict[str, Any]:
        prices = first_variant.get("prices") or []
        base = 0.0
        currency = ""
        if isinstance(prices, list) and prices:
            first = prices[0] or {}
            try:
                amount = first.get("amount")
                base = round(float(amount or 0) / 100.0, 4)
            except (TypeError, ValueError):
                base = 0.0
            currency = _ns(first.get("currency_code"))

        # Channel-tier prices in Medusa land on price-list entries
        # bound to sales channels; reconciling those against ERP
        # price lists is a Phase 5 concern. For now we emit an empty
        # list, which matches what ``_canonical_pricing`` does on the
        # ERP side when the sync has no per-channel overrides.
        # TODO: when sync.price_strategy = channel_price_list with
        # per-channel rows, expand price-list rules and emit one
        # channel_prices entry per matched rule.
        return {
            "currency": currency,
            "base_price": base,
            "channel_prices": [],
        }

    def _live_inventory(
        self, first_variant: dict[str, Any],
    ) -> dict[str, Any]:
        raw = first_variant.get("inventory_quantity")
        try:
            qty = round(float(raw or 0), 4)
        except (TypeError, ValueError):
            qty = 0.0
        return {"qty": qty}

    def _live_images(
        self, product: dict[str, Any],
    ) -> list[dict[str, Any]]:
        # Medusa carries the cover image as ``thumbnail`` separately
        # from the ``images[]`` collection. Match the engine-side
        # convention of marking the first sorted URL as primary —
        # that way a hash stays stable across re-uploads that don't
        # change the URL set.
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for img in product.get("images") or []:
            if not isinstance(img, dict):
                continue
            url = img.get("url")
            if not url or url in seen:
                continue
            seen.add(url)
            out.append({"url": url, "primary": False})
        out.sort(key=lambda d: d["url"])
        if out:
            out[0]["primary"] = True
        return out

    def _live_properties(
        self, metadata: dict[str, Any],
    ) -> dict[str, Any]:
        # Medusa doesn't carry first-class brand/manufacturer fields
        # on the product entity. The convention this plugin uses is
        # to stash them on ``metadata`` (a free-form JSON map) under
        # operator-chosen keys. We read ``brand``/``manufacturer``
        # there; if the operator picked different keys, the hash
        # will diverge until Phase 5 makes the keys configurable.
        # TODO: make the metadata keys configurable on the sync doc.
        return {
            "brand": _ns(metadata.get("brand")),
            "manufacturer": _ns(metadata.get("manufacturer")),
            "attributes": [],
        }

    def _live_seo(
        self,
        product: dict[str, Any],
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "slug": _ns(product.get("handle")),
            "meta_title": _ns(metadata.get("meta_title")),
            "meta_description": _ns(metadata.get("meta_description")),
        }

    def _live_taxes(self) -> dict[str, Any]:
        # Medusa tax handling lives on tax-region/tax-rate objects
        # rather than directly on the product. The engine-side
        # canonical reports the operator-chosen Item Tax Template
        # name, which has no direct equivalent on the live side.
        # Emit empty string so the hash compares against the ERP
        # "no template set" case; Phase 5 will map tax-rate ids
        # back to a comparable string.
        # TODO: resolve tax_rate_id → template name for parity.
        return {"template": ""}

    def _live_categories(
        self, metadata: dict[str, Any],
    ) -> dict[str, Any]:
        # ERP side hashes the Item Group name, not the backend
        # category id; category-tree drift is the Catalog Mirror's
        # job, not this adapter's. We read an operator-stashed
        # ``item_group`` key from ``metadata`` so that hash parity
        # converges when the operator opts into round-tripping IG
        # names through metadata; otherwise we emit empty string,
        # matching the ERP "no IG set" case.
        # TODO: when sync.match_categories_by_name is enabled, walk
        # the categories association and emit the first IG name match.
        return {"item_group": _ns(metadata.get("item_group"))}


# ─── Module-level helpers ────────────────────────────────────────────


def _matches_filters(
    node: LiveProductNode,
    *,
    sales_channel_ids: list[str] | None,
    category_ids: list[str] | None,
) -> bool:
    if sales_channel_ids:
        wanted_channels = set(sales_channel_ids)
        if not wanted_channels.intersection(node.sales_channel_ids or []):
            return False
    if category_ids:
        wanted_categories = set(category_ids)
        if not wanted_categories.intersection(node.category_ids or []):
            return False
    return True


def _ns(value) -> str:
    """Normalise to single-space-collapsed stripped string.

    Mirrors :func:`product_sync.engine.canonical._norm_str` so a value
    that the ERP-side hashes as ``"Acme GmbH"`` doesn't hash as
    ``"  Acme  GmbH "`` on the live side. ``None`` becomes empty
    string, which is what the canonical builder also does.
    """
    if value is None:
        return ""
    s = str(value)
    if not s:
        return ""
    return " ".join(s.split())


def _live_flag(sync, name: str, *, default: bool) -> bool:
    """Toggle reader that matches ``canonical._flag``.

    Importing the private symbol directly from ``canonical`` would
    couple this module to an underscore-prefixed name; reimplementing
    is one line and keeps the boundary clean.
    """
    val = getattr(sync, name, None)
    if val is None:
        return default
    return bool(int(val) if isinstance(val, (str, int, float)) else val)
