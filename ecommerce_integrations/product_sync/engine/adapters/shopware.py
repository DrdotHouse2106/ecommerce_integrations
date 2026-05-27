"""Shopware ProductAdapter — Product Sync ↔ Shopware product catalogue.

Wraps the Shopware Admin API in the contract laid out by
:class:`ProductAdapter`. The adapter is intentionally thin: it does
one thing — speak HTTP — and leaves orchestration (batching, retry
policy, error routing) to the caller.

Three patterns are inherited verbatim from
:mod:`ecommerce_integrations.catalog_mirror.engine.adapters.shopware`
because they are session-level concerns, not endpoint-level:

1. **Session wrapping** — every public method funnels through
   :func:`_with_client`, which wraps the call in
   ``temp_shopware_session``. That handles auth, gateway-retry, and
   idempotency-key injection on writes for free.
2. **Pagination loop** — ``page/limit`` with abort-on-short-page.
3. **Defensive errors** — every API call sits inside a ``try`` that
   maps the SDK exception to :class:`AdapterError` with ``__cause__``
   preserved, so the orchestrator's log row has the original
   traceback.

Live-side hashing (:meth:`ShopwareProductAdapter.compute_live_hash`)
re-uses the canonical-payload section builders from
:mod:`product_sync.engine.canonical` deliberately: if the live-side
hash is computed against a different shape than the ERP-side hash,
the differ would loop forever ("they don't match — push — still
don't match — push"). The section keys produced here must remain in
lock-step with ``_canonical_basic`` / ``_canonical_pricing`` / etc.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import frappe

from ecommerce_integrations.product_sync.constants import BACKEND_SHOPWARE
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
from ecommerce_integrations.shopware6.connection import temp_shopware_session

# Shopware caps page size at 500. We use the cap on big fetches so we
# pay fewer round-trips on a five-figure catalogue.
_FETCH_PAGE_SIZE = 500

# Bulk-sync chunk size. Shopware accepts more but throughput peaks
# around 100 and lock-timeout risk climbs above that — same number the
# existing BatchProductUploader settled on.
_BULK_CHUNK_SIZE = 100

# Match-workflow result cap. UI shows the top handful; anything more
# is noise and just inflates the round-trip.
_MATCH_LIMIT = 10

# Shopware "visible everywhere" — search + listing + direct URL. The
# enum value is fixed in the entity schema; 10 = link only, 20 = link
# + search, 30 = full. We push the maximum because Product Sync owns
# the per-channel decision via the include/exclude list; once a
# channel is included, the operator wants the product fully visible.
_VISIBILITY_ALL = 30


class ShopwareProductAdapter(ProductAdapter):
    """Concrete adapter against Shopware 6 Admin API.

    All five public methods are wrappers that delegate to private
    ``_*_impl`` methods which run inside a Shopware session. The
    indirection is what lets tests inject a mock client without going
    through OAuth.
    """

    backend = BACKEND_SHOPWARE

    # ─── Public adapter surface ──────────────────────────────────────

    def fetch_products(
        self,
        *,
        sales_channel_ids: list[str] | None = None,
        category_ids: list[str] | None = None,
        external_ids: list[str] | None = None,
    ) -> Iterator[LiveProductNode]:
        # Iterator pattern: yield per-page, not all-at-once. A 50k
        # catalogue blown into memory is what crashed the legacy
        # exporter; this adapter must not repeat that mistake.
        yield from _with_client(
            self._fetch_products_impl,
            sales_channel_ids=sales_channel_ids,
            category_ids=category_ids,
            external_ids=external_ids,
        )

    def upsert_products_bulk(
        self,
        items: list[dict],
        *,
        target_sales_channels: list[str],
    ) -> list[dict]:
        """Push N products via ``POST /_action/sync`` in ~25-item chunks.

        Reuses the internal ``_bulk_upsert`` (which already chunks via
        ``_BULK_CHUNK_SIZE`` and parses per-item errors). One outer
        session wraps the whole batch instead of one session per item
        — that's the wall-time win.
        """
        if not items:
            return []
        prepared = [
            self._prepare_upsert_payload(
                external_id=entry.get("external_id"),
                payload=entry.get("payload") or {},
                target_sales_channels=target_sales_channels,
            )
            for entry in items
        ]

        def _resolve_then_bulk(client):
            self._resolve_delivery_time_ids(client, prepared)
            return self._bulk_upsert(client, prepared)

        return _with_client(_resolve_then_bulk)

    def ensure_property_entities_bulk(
        self,
        groups: list[tuple[str, str]],
    ) -> dict[str, str]:
        """Bulk-upsert ``property_group`` + ``property_group_option``
        rows so a subsequent product upsert can reference them via
        the m2m link without per-item DB lookups.

        ``groups`` is a list of ``(name, kind)`` tuples where ``kind``
        is ``"property"`` (filterable property) or ``"variant"`` (used
        for variant configurator). The two kinds intentionally produce
        different option UUIDs (matching the legacy
        ``get_or_create_property_option`` vs ``get_or_create_variant_option``
        schemes) so a value like "RAL 7035" can exist twice — once as
        a filterable property, once as a variant option — without
        collision.

        Each upsert also writes ``property_group.position`` derived
        from :func:`product_sync.engine.property_classifier.property_priority`
        so the storefront PDP groups appear in the same order across
        every product (lower priority = earlier on the page). Shopware
        does not support per-product property order, so this global
        position is the closest equivalent to "ERPNext sets the idx
        and Shopware respects it".

        Returns a dict mapping ``f"{kind}:{name}"`` → group_uuid so
        callers can derive option UUIDs themselves via
        ``property_option_uuid(name, value, kind)``. (The option UUIDs
        are pure functions of the inputs, no DB roundtrip needed.)
        """
        if not groups:
            return {}

        from ecommerce_integrations.shopware6.export.utils import (
            generate_uuid,
        )
        from ecommerce_integrations.product_sync.engine.property_classifier import (
            property_priority,
        )

        rows = []
        result: dict[str, str] = {}
        for name, kind in groups:
            gid = generate_uuid(f"property_group_{name}")
            rows.append({
                "id": gid,
                "name": name,
                "displayType": "text",
                "sortingType": "alphanumeric",
                "filterable": True,
                "visibleOnProductDetailPage": True,
                "position": property_priority(name),
            })
            result[f"{kind}:{name}"] = gid

        def go(client):
            client.request_post("_action/sync", payload={
                "groups": {
                    "entity": "property_group",
                    "action": "upsert",
                    "payload": rows,
                },
            })
            return result

        return _with_client(go)

    def ensure_property_options_bulk(
        self,
        options: list[tuple[str, str, str]],
    ) -> None:
        """Bulk-upsert ``property_group_option`` rows so the subsequent
        product upsert's nested ``properties`` / ``options`` m2m link
        finds them. ``options`` is a list of ``(group_name, value, kind)``
        tuples; ``kind`` selects the same UUID scheme the legacy
        per-call helper used (so existing rows are matched, not
        duplicated)."""
        if not options:
            return

        from ecommerce_integrations.shopware6.export.utils import (
            generate_uuid,
        )

        rows = []
        for group_name, value, kind in options:
            group_id = generate_uuid(f"property_group_{group_name}")
            prefix = "variant_option" if kind == "variant" else "property_option"
            opt_id = generate_uuid(f"{prefix}_{group_name}_{value}")
            rows.append({"id": opt_id, "groupId": group_id, "name": value})

        def go(client):
            # ``upsert`` semantics: existing rows are matched by id and
            # the name field is rewritten (harmless — same value).
            client.request_post("_action/sync", payload={
                "options": {
                    "entity": "property_group_option",
                    "action": "upsert",
                    "payload": rows,
                },
            })

        _with_client(go)

    def upsert_product(
        self,
        *,
        external_id: str | None,
        payload: dict,
        target_sales_channels: list[str],
    ) -> str:
        return _with_client(
            self._upsert_product_impl,
            external_id=external_id,
            payload=payload,
            target_sales_channels=target_sales_channels,
        )

    def reconcile_product_media(
        self,
        external_id: str,
        keep_media_ids: list[str],
    ) -> dict:
        """Reconcile a product's media set against the keep list.

        Three responsibilities, all idempotent:

        1. **Delete stale product_media rows.** Any live row whose
           media UUID isn't in ``keep_media_ids`` is removed. Without
           this every re-sync only ADDS media — the storefront
           accumulates orphan photos across runs (replaced product
           photography that wasn't unlinked, leftovers from prior
           mis-mappings, etc.).
        2. **Ensure ``product.coverId`` points at a real row.** Sets
           the cover to the first survivor when the prior cover was on
           a deleted row OR when no cover was ever set. Without this
           the Store API returns ``cover: null`` and the storefront
           falls back to a placeholder even though media exist.
        3. **Retrofit ``media.mediaFolderId``.** Media records created
           before the folder fix landed in Shopware's "Unsorted"
           bucket, which suppresses thumbnail generation. Moves them
           into the resolved Product Media folder.

        ``keep_media_ids`` is what the caller wants preserved — for
        the standard flow this is the values list from
        ``Ecommerce Item.pushed_image_map`` (= only media that this
        sync has pushed). Callers that want to preserve admin uploads
        should fold those ids into the keep list before invoking.

        Returns ``{deleted, kept, failed, cover_set, folder_set}``.
        """
        return _with_client(
            self._reconcile_media_impl,
            external_id=external_id,
            keep_media_ids=set(keep_media_ids or ()),
        )

    def upload_product_images(
        self,
        external_id: str,
        image_urls: list[str],
    ) -> dict:
        """Two-step image upload — Shopware-specific.

        Shopware's ``_action/sync`` endpoint with ``media`` field only
        creates empty Media records; it does NOT trigger the URL
        download. The correct API is ``POST /_action/media/{id}/upload``
        with ``?fileName=&extension=`` and JSON body ``{"url": "..."}``.

        Each call:
        1. Generates a fresh ``media`` UUID and creates the empty record
           via the sync endpoint.
        2. Hits ``/_action/media/{id}/upload`` with the URL to trigger
           the async fetch.
        3. Links the media to ``external_id`` via ``product_media``.

        Returns ``{uploaded, failed, skipped}`` counters. Failures are
        logged + counted; we never raise from here, so a single broken
        image doesn't poison the rest of the apply.

        Idempotency: callers should compare canonical vs live image
        hashes BEFORE invoking — this method always uploads regardless,
        creating new media entities each call.
        """
        return _with_client(
            self._upload_images_impl,
            external_id=external_id,
            image_urls=list(image_urls or []),
        )

    def push_variant_options(
        self,
        external_id: str,
        attribute_values: list[dict],
    ) -> dict:
        """Assign a variant's option ids so Shopware groups it under its
        template's configurator.

        Without this every child product (``parentId`` set) shows up as
        a separate PDP — the storefront doesn't render the option
        selector because the m2m ``product_option`` table is empty.

        ``attribute_values`` is a list of ``{"name": <attr>, "value":
        <val>}`` dicts in the same shape the ``Item Variant Attribute``
        child rows expose. Resolves each via the existing
        ``get_or_create_variant_option`` helper so the same property
        group + option entities are shared with the template's
        configurator settings.

        Returns ``{set, failed, skipped, option_ids}`` — caller uses
        ``option_ids`` to drive the template's
        ``push_template_configurator``.
        """
        return _with_client(
            self._push_variant_options_impl,
            external_id=external_id,
            attribute_values=list(attribute_values or []),
        )

    def push_template_configurator(
        self,
        external_id: str,
        option_ids: list[str],
    ) -> dict:
        """Wire a template's ``configuratorSettings`` so the storefront
        renders the variant-selector on its PDP.

        Without this the template is a regular product with hidden
        ``parentId=null`` children but no UI for the customer to pick a
        variant. Idempotent via deterministic
        ``product_configurator_setting.id`` (md5 of product_id +
        option_id) — re-running doesn't duplicate rows.
        """
        return _with_client(
            self._push_template_configurator_impl,
            external_id=external_id,
            option_ids=list(option_ids or []),
        )

    def push_product_properties(
        self,
        external_id: str,
        properties: list[dict],
    ) -> dict:
        """Sync ``ecommerce_properties`` to Shopware property groups.

        Shopware models a "property" as ``property_group`` → many
        ``property_group_option`` rows; products reference one option
        per (group, value) pair through the ``properties`` m2m link.
        This helper resolves both — creating the group / option when
        missing — then patches the product with the option ids so the
        storefront's "Eigenschaften" tab populates.

        ``properties`` is a list of ``{"name": <group>, "value":
        <option>}`` dicts in the same shape the canonical builder
        emits via ``Item.ecommerce_properties`` (``property_name`` /
        ``property_value`` on the child row).

        Returns ``{linked, failed, skipped}``. A missing/blank value
        is counted as skipped — bools / numbers must be stringified
        by the caller (canonical does this) so Shopware accepts them
        as ``property_group_option.name``.
        """
        return _with_client(
            self._push_properties_impl,
            external_id=external_id,
            properties=list(properties or []),
        )

    def deactivate_product(self, external_id: str) -> None:
        _with_client(self._deactivate_impl, external_id)

    def delete_product(self, external_id: str) -> None:
        _with_client(self._delete_impl, external_id)

    def find_matching_products(
        self,
        *,
        sku: str | None = None,
        ean: str | None = None,
        name: str | None = None,
    ) -> list[ProductMatch]:
        # Note: the ABC only declares ``sku``/``name``; ``ean`` is an
        # extra keyword the Shopware adapter accepts because Shopware
        # carries EAN natively on the product entity. Keyword-only +
        # default ``None`` keeps the override covariant with the base.
        return _with_client(
            self._find_matching_impl,
            sku=sku,
            ean=ean,
            name=name,
        )

    def find_matching_products_bulk(
        self,
        *,
        skus: list[str] | None = None,
        eans: list[str] | None = None,
    ) -> dict[str, ProductMatch]:
        # Shopware's ``equalsAny`` filter takes a list — we send N
        # lookup values in one ``POST /search/product`` instead of N
        # HTTP calls. SKUs are checked against ``productNumber``, EANs
        # against ``ean``. SKU hits win when both fire for the same
        # backend product.
        return _with_client(
            self._find_matching_bulk_impl,
            skus=skus,
            eans=eans,
        )

    # ─── Hashing parity with engine/canonical.py ─────────────────────

    def compute_live_hash(self, product: dict[str, Any], sync) -> str:
        """Hash a live Shopware product the same way the ERP side hashes
        the source Item, so the differ can compare apples to apples.

        ``product`` is the raw response dict as it appears in the
        ``data[]`` array of ``POST /search/product`` — the same shape
        that backs :class:`LiveProductNode`. ``sync`` is the
        ``Ecommerce Product Sync`` doc whose toggles decide which
        sections land in the payload.

        The output dict mirrors :func:`build_canonical_payload` by
        construction: same top-level keys, same section schemas, same
        sort orders. Any divergence and the differ flaps.
        """
        payload = self._build_live_canonical(product, sync)
        return compute_hash(payload)

    # ─── Implementations (run inside temp_shopware_session) ──────────

    def _fetch_products_impl(
        self,
        client,
        *,
        sales_channel_ids: list[str] | None,
        category_ids: list[str] | None,
        external_ids: list[str] | None,
    ) -> Iterator[LiveProductNode]:
        # Bulk-by-IDs path: when ``external_ids`` is set we know the
        # exact universe up front, so we can chunk it into 500-ID
        # ``equalsAny`` batches (Shopware's filter cap) and fire the
        # batches in parallel. Each batch is exactly one page, so no
        # inner pagination is needed. ``client`` comes from the
        # surrounding ``temp_shopware_session`` and is reused across
        # threads (the SDK wraps a ``requests.Session`` which is
        # thread-safe for independent calls).
        if external_ids:
            yield from self._fetch_by_ids_parallel(client, external_ids)
            return

        # Fallback: filtered walk with serial pagination. Used for
        # orphan detection (``sales_channel_ids`` / ``category_ids``)
        # where the total count is unknown up front.
        filters = self._build_fetch_filters(
            sales_channel_ids=sales_channel_ids,
            category_ids=category_ids,
            external_ids=None,
        )
        page = 1
        while True:
            criteria = self._build_fetch_criteria(page=page, filters=filters)
            try:
                resp = client.request_post(
                    "search/product", payload=criteria,
                )
            except Exception as e:
                raise AdapterError(
                    f"Shopware product search failed (page={page}): {e}",
                ) from e

            data = (resp or {}).get("data") or []
            if not data:
                return
            for row in data:
                node = self._row_to_live_node(row)
                if node is not None:
                    yield node
            if len(data) < _FETCH_PAGE_SIZE:
                return
            page += 1

    def _fetch_by_ids_parallel(
        self, client, external_ids: list[str],
    ) -> Iterator[LiveProductNode]:
        """Fire one HTTP request per 500-ID chunk, serially.

        Despite the historical name, this path is now serial after
        we hit two thread-related issues in production:

        - ``frappe.flags`` (thread-local) isn't auto-propagated to
          ``ThreadPoolExecutor`` workers, so the Shopware SDK's retry
          path crashed inside threads with "object is not bound".
        - Even after passing the client through, a multi-minute
          IO-bound fetch left the worker's shared MySQL connection
          idle long enough for ``wait_timeout`` to fire — the next
          post-fetch DB write died with ``(2006)``. MySQLdb
          connections aren't safe to share across worker threads in
          unrelated code paths anyway.

        The chunking itself is still the win: each chunk is one HTTP
        roundtrip via ``equalsAny`` (no inner pagination), so wall
        time scales with N / 500 instead of the old page-by-page walk.
        On a 33 961-item drift fetch that's still ~10 × faster than
        the pre-restructure path because the per-page overhead is
        gone — and we don't have to reason about thread-vs-Frappe
        edge cases.
        """
        cleaned = sorted({eid for eid in external_ids if eid})
        if not cleaned:
            return
        for i in range(0, len(cleaned), _FETCH_PAGE_SIZE):
            yield from self._fetch_chunk(
                client, cleaned[i : i + _FETCH_PAGE_SIZE],
            )

    def _fetch_chunk(self, client, chunk: list[str]) -> Iterator[LiveProductNode]:
        """Fetch one chunk (max 500 IDs) using the supplied client."""
        criteria = self._build_fetch_criteria(
            page=1,
            filters=[{
                "type": "equalsAny",
                "field": "id",
                "value": list(chunk),
            }],
        )
        try:
            resp = client.request_post("search/product", payload=criteria)
        except Exception as e:
            raise AdapterError(
                f"Shopware product chunk fetch failed (size={len(chunk)}): {e}",
            ) from e
        for row in (resp or {}).get("data") or []:
            node = self._row_to_live_node(row)
            if node is not None:
                yield node

    def _build_fetch_criteria(
        self, *, page: int, filters: list[dict[str, Any]] | None,
    ) -> dict[str, Any]:
        """One place that owns the canonical ``search/product`` payload.

        Associations carry the data the live hash needs. Drop an
        association and the live-hash silently goes stale (e.g.
        "no images on live, push images every run").
        """
        criteria: dict[str, Any] = {
            "page": page,
            "limit": _FETCH_PAGE_SIZE,
            "associations": {
                "tax": {},
                "media": {"associations": {"media": {}}},
                "properties": {"associations": {"group": {}}},
                "prices": {},
                "visibilities": {},
                "categories": {},
                # Manufacturer carries the brand name. Without this
                # ``_live_properties`` returned ``brand=""`` hardcoded
                # and 100 % of items showed a brand-drift on every
                # preview, even when ERPNext's ``Item.brand`` and the
                # Shopware ``manufacturer.name`` already agreed.
                "manufacturer": {},
            },
        }
        if filters:
            criteria["filter"] = filters
        return criteria

    def _build_fetch_filters(
        self,
        *,
        sales_channel_ids: list[str] | None,
        category_ids: list[str] | None,
        external_ids: list[str] | None,
    ) -> list[dict[str, Any]]:
        """AND-combine the three filter dimensions.

        Each non-empty list becomes one ``equalsAny`` clause; the API
        AND-combines the top-level ``filter`` array, which gives us the
        right semantics with no nesting.
        """
        filters: list[dict[str, Any]] = []
        if external_ids:
            filters.append({
                "type": "equalsAny",
                "field": "id",
                "value": list(external_ids),
            })
        if sales_channel_ids:
            filters.append({
                "type": "equalsAny",
                "field": "visibilities.salesChannelId",
                "value": list(sales_channel_ids),
            })
        if category_ids:
            # ``categoriesRo`` is the read-only join including parent
            # categories, which is what the operator means when they
            # filter by "anything under this branch".
            filters.append({
                "type": "equalsAny",
                "field": "categoriesRo.id",
                "value": list(category_ids),
            })
        return filters

    def _row_to_live_node(self, row: dict[str, Any]) -> LiveProductNode | None:
        # Shopware responses come in two shapes depending on version
        # and includes: flat (everything top-level) or attributes-wrapped.
        # Accept both and let the rest of the method read from a single
        # merged view.
        attrs = row.get("attributes") or {}
        merged: dict[str, Any] = {**attrs, **{k: v for k, v in row.items() if k != "attributes"}}

        ext_id = merged.get("id")
        if not ext_id:
            return None

        translated = merged.get("translated") or {}
        name = merged.get("name") or translated.get("name") or ""
        description = merged.get("description") or translated.get("description") or None

        # Price: take the first list price. The full multi-currency
        # picture is a Phase-5 concern; Phase-2 reports the primary.
        price_val: float | None = None
        prices = merged.get("price") or []
        if isinstance(prices, list) and prices:
            first = prices[0] or {}
            try:
                price_val = float(first.get("gross") or first.get("net") or 0) or None
            except (TypeError, ValueError):
                price_val = None

        # Stock: ``availableStock`` is the customer-facing number;
        # ``stock`` is the warehouse number. Prefer availableStock so
        # the live-hash matches what shoppers see.
        stock_raw = merged.get("availableStock")
        if stock_raw is None:
            stock_raw = merged.get("stock")
        try:
            stock_val: int | None = int(stock_raw) if stock_raw is not None else None
        except (TypeError, ValueError):
            stock_val = None

        # Categories: prefer the expanded ``categories`` association
        # (each entry has an id); fall back to the inline ``categoryIds``
        # array Shopware emits when no association was requested.
        cat_ids: list[str] = []
        cats = merged.get("categories")
        if isinstance(cats, list) and cats:
            for c in cats:
                cid = (c or {}).get("id") if isinstance(c, dict) else None
                if cid:
                    cat_ids.append(cid)
        if not cat_ids:
            raw_cat_ids = merged.get("categoryIds") or []
            if isinstance(raw_cat_ids, list):
                cat_ids = [str(x) for x in raw_cat_ids if x]
        cat_ids = sorted(set(cat_ids))

        # Sales channels from visibilities — same shape as the catalog
        # mirror's visibility check.
        sc_ids: list[str] = []
        for vis in merged.get("visibilities") or []:
            if not isinstance(vis, dict):
                continue
            sc_id = vis.get("salesChannelId")
            if sc_id:
                sc_ids.append(sc_id)
        sc_ids = sorted(set(sc_ids))

        # Image URLs + media UUIDs. Shopware nests as ``media[].media.url``
        # with the media id at ``media[].mediaId`` (or ``media[].media.id``).
        # We keep both in parallel so the differ can match the
        # ``pushed_image_map`` (ERP basename → media UUID) against the
        # live id set — which is necessary because Shopware renames
        # uploaded files to ``<sku>_<idx>.jpg``, so URL-basename
        # matching against the ERP filename always fails.
        images: list[str] = []
        image_ids: list[str] = []
        for m in merged.get("media") or []:
            if not isinstance(m, dict):
                continue
            inner = m.get("media") or {}
            url = inner.get("url") if isinstance(inner, dict) else None
            mid = (
                m.get("mediaId")
                or (inner.get("id") if isinstance(inner, dict) else None)
            )
            if url:
                images.append(url)
            if mid:
                image_ids.append(mid)
        # Sort URLs for stable hashing; keep image_ids unsorted (they
        # don't anchor the hash and the order isn't load-bearing for
        # the differ's set-comparison).
        images = sorted(set(images))

        # Build the per-field properties + SEO snapshots from the same
        # builders the live-canonical uses; this gives the differ a
        # consistent shape to compare against the proposed canonical.
        properties_dict = self._live_properties(merged)
        seo_dict = self._live_seo(merged)

        return LiveProductNode(
            external_id=ext_id,
            sku=merged.get("productNumber") or None,
            name=name,
            description=description,
            price=price_val,
            stock=stock_val,
            category_ids=cat_ids,
            sales_channel_ids=sc_ids,
            active=bool(merged.get("active", True)),
            images=images,
            image_ids=image_ids,
            properties=properties_dict,
            seo=seo_dict,
        )

    def _upsert_product_impl(
        self,
        client,
        *,
        external_id: str | None,
        payload: dict,
        target_sales_channels: list[str],
    ) -> str:
        prepared = self._prepare_upsert_payload(
            external_id=external_id,
            payload=payload,
            target_sales_channels=target_sales_channels,
        )
        self._resolve_delivery_time_ids(client, [prepared])
        results = self._bulk_upsert(client, [prepared])
        if not results:
            raise AdapterError(
                "Shopware sync returned no result for upsert",
            )
        first = results[0]
        if first.get("error"):
            raise AdapterError(
                f"Shopware product upsert failed: {first['error']}",
            )
        # Shopware echoes the id on insert/update either way — but our
        # _prepare ensured ``id`` is always set on the outgoing payload.
        # The "look in the response first, fall back to outgoing" order
        # matters when the server normalised the UUID.
        return first.get("external_id") or prepared.get("id")

    def _bulk_upsert(
        self, client, payloads: list[dict],
    ) -> list[dict[str, Any]]:
        """Push ``payloads`` via ``POST /_action/sync`` in chunks.

        Returns one result dict per input payload, in input order:
        ``{external_id, error}``. ``error`` is ``None`` on success.

        The orchestrator (Phase 5) will call this directly through a
        public ``upsert_products_bulk`` wrapper that lives one layer
        above; for now ``upsert_product`` is the only caller.
        """
        results: list[dict[str, Any]] = []
        for i in range(0, len(payloads), _BULK_CHUNK_SIZE):
            chunk = payloads[i:i + _BULK_CHUNK_SIZE]
            sync_payload = {
                "products": {
                    "entity": "product",
                    "action": "upsert",
                    "payload": chunk,
                },
            }
            try:
                resp = client.request_post(
                    "_action/sync", payload=sync_payload,
                )
            except Exception as e:
                # Whole-chunk failure: surface the original error and
                # mark every item in the chunk failed. The orchestrator
                # decides retry vs. give-up per-item from here.
                msg = f"Shopware bulk sync failed: {e}"
                for p in chunk:
                    results.append({
                        "external_id": p.get("id"),
                        "error": msg,
                    })
                raise AdapterError(msg) from e

            # Parse per-item errors out of the response body. Shopware's
            # sync endpoint returns ``{success: bool, data: {<key>:
            # {result: [{errors: [...]}]}}}`` — same shape as the legacy
            # batch uploader unpacks.
            chunk_errors = self._extract_per_item_errors(resp, len(chunk))
            for idx, p in enumerate(chunk):
                err = chunk_errors[idx] if idx < len(chunk_errors) else None
                results.append({
                    "external_id": p.get("id"),
                    "error": err,
                })

            # If the response declared overall failure but didn't break
            # out per-item, raise so the caller doesn't silently treat
            # the chunk as a success.
            if (resp or {}).get("success") is False and not any(chunk_errors):
                raise AdapterError(
                    f"Shopware bulk sync reported failure: {resp!r}",
                )
        return results

    def _extract_per_item_errors(
        self, resp: dict[str, Any] | None, chunk_size: int,
    ) -> list[str | None]:
        """Pull per-item error strings out of a sync-API response.

        Returns a list of length ``chunk_size``; entries are ``None``
        when the item succeeded, otherwise a short string carrying the
        Shopware-side message (best-effort; the API's error shapes vary).
        """
        errs: list[str | None] = [None] * chunk_size
        data = (resp or {}).get("data") or {}
        block = data.get("products") or {}
        result = block.get("result") or []
        for idx, row in enumerate(result):
            if idx >= chunk_size:
                break
            row_errors = (row or {}).get("errors") or []
            if row_errors:
                # Compact a list of error dicts into one human-readable
                # string. Shopware errors carry ``code``/``detail``.
                parts: list[str] = []
                for e in row_errors[:3]:
                    if isinstance(e, dict):
                        detail = e.get("detail") or e.get("title") or e.get("code") or str(e)
                        parts.append(str(detail))
                    else:
                        parts.append(str(e))
                errs[idx] = "; ".join(parts) or "unknown error"
        return errs

    def _resolve_delivery_time_ids(self, client, prepared: list[dict]) -> None:
        """Substitute ``deliveryTime`` strings with ``deliveryTimeId`` uuids
        in-place across a batch of prepared payloads.

        The canonical payload carries the free-form string
        ("10-15 Tage") so canonical stays I/O-free; this helper resolves
        each distinct string to its Shopware ``delivery_time`` uuid once
        per batch via ``get_or_create_delivery_time``. Same session as
        the bulk push so a single OAuth handshake covers both.
        """
        from ecommerce_integrations.shopware6.export.product_mapper import (
            get_or_create_delivery_time,
        )
        cache: dict[str, str] = {}
        for p in prepared:
            name = (p.pop("deliveryTime", None) or "").strip()
            if not name:
                continue
            dt_id = cache.get(name)
            if dt_id is None:
                dt_id = get_or_create_delivery_time(client, name) or ""
                cache[name] = dt_id
            if dt_id:
                p["deliveryTimeId"] = dt_id

    def _prepare_upsert_payload(
        self,
        *,
        external_id: str | None,
        payload: dict,
        target_sales_channels: list[str],
    ) -> dict[str, Any]:
        """Normalise an outgoing product payload for the sync endpoint.

        - Sets ``id`` on update. When ``external_id`` is omitted we
          leave id-generation to Shopware; the response will surface
          the assigned id.
        - Injects ``visibilities`` from ``target_sales_channels`` so
          Product Sync's per-channel decision wins over whatever is
          currently on the product.
        - Normalises ``prices[i].quantityStart`` (the JTL edge case —
          quantityStart=0 was tolerated pre-6.7 but rejects now).
        """
        out: dict[str, Any] = dict(payload or {})
        if external_id:
            out["id"] = external_id
        else:
            # Pre-generate the id on CREATE so the caller can read it
            # back from the prepared payload — Shopware's
            # ``/_action/sync`` endpoint returns a sparse response that
            # does NOT echo new ids, so without this the orchestrator
            # records the create as "no external_id from backend" and
            # leaves the Ecommerce Item mapping unset (forcing the next
            # sync to try the create again, which then collides with
            # the now-existing product).
            import uuid as _uuid
            out["id"] = _uuid.uuid4().hex

        # Visibilities — only set when caller actually passed channels.
        # Empty list means "no opinion", not "remove all visibilities".
        #
        # Critical: only set on CREATE (no external_id). On UPDATE the
        # product already has a row in ``product_visibility`` for the
        # target channel; re-sending that row through ``/api/_action/sync``
        # triggers a fresh INSERT (the bulk endpoint does not upsert
        # m2m rows by natural key, even with an explicit id supplied),
        # which trips
        # ``1062 Duplicate entry … uniq.product_id__sales_channel_id``.
        # Because /sync is atomic per call, ONE collision aborts the
        # entire 25-item batch — so a single re-sync run can land
        # every batch in FailedJobRegistry.
        #
        # Visibility *changes* (channel added/removed for an item) are
        # handled out-of-band by the channel-assignment helper, not the
        # bulk upsert, so dropping them here on UPDATE is safe.
        if external_id:
            # Update path: strip any inherited visibilities (the
            # canonical payload builder sets one row per target
            # channel by default — see ``engine/payload.py``). Re-
            # sending those triggers the unique-key collision
            # described above.
            out.pop("visibilities", None)
        elif target_sales_channels:
            # Create path: replace any inherited visibilities with the
            # caller's authoritative target_sales_channels list so the
            # new product is born with the right channel set.
            out["visibilities"] = [
                {
                    "salesChannelId": sc_id,
                    "visibility": _VISIBILITY_ALL,
                }
                for sc_id in target_sales_channels
                if sc_id
            ]

        # Edge-case from JTL repo: quantityStart < 1 is rejected by
        # Shopware ≥6.7. Normalise upward instead of dropping the row,
        # because dropping it would change pricing semantics; bumping
        # to 1 keeps the price tier in place.
        prices = out.get("prices")
        if isinstance(prices, list):
            for tier in prices:
                if not isinstance(tier, dict):
                    continue
                qs = tier.get("quantityStart")
                try:
                    if qs is None or float(qs) < 1:
                        tier["quantityStart"] = 1
                except (TypeError, ValueError):
                    tier["quantityStart"] = 1
        return out

    def _upload_images_impl(
        self,
        client,
        *,
        external_id: str,
        image_urls: list[str],
    ) -> dict:
        """Inner image-upload — runs under ``temp_shopware_session``.

        Returns ``{uploaded, failed, skipped, media_map}``. ``media_map``
        is ``{erp_basename: shopware_media_uuid}`` for successful uploads,
        which the orchestrator persists on ``Ecommerce Item.
        pushed_image_map`` so the next preview can short-circuit the
        image diff.
        """
        import uuid

        counters = {"uploaded": 0, "failed": 0, "skipped": 0, "media_map": {}}
        if not external_id or not image_urls:
            return counters

        # Use the library's own ``request_post`` with
        # ``additional_query_params``. The earlier "bypass the SDK
        # and hit session.post directly" workaround broke OAuth: the
        # SDK injects the Bearer token through an authlib auth
        # handler that's bound to its request methods, not to the
        # underlying httpx session. Direct ``session.post()`` calls
        # therefore went out without ``Authorization`` and Shopware
        # rejected every upload with 401 — silently, because the
        # adapter only counted the failures. ``additional_query_params``
        # lets us send the required ``fileName`` / ``extension`` query
        # string without dropping out of the auth-aware code path.

        # Resolve the Product Media folder once per batch. Without an
        # explicit ``mediaFolderId`` the created media entity lands in
        # Shopware's "Unsorted" bucket, which the admin UI then surfaces
        # under a generic root instead of the product-media section
        # (and breaks thumbnail-config / file-rules that hang off the
        # folder). ``_resolve_product_media_folder_id`` returns the
        # default Shopware-shipped "Product Media" folder id or — if
        # the operator renamed/removed it — falls back to None so we
        # stay backwards-compatible with the legacy behaviour.
        media_folder_id = _resolve_product_media_folder_id(client)

        new_media_ids: list[str] = []
        for url in image_urls:
            url = (url or "").strip()
            if not url:
                counters["skipped"] += 1
                continue
            # 1. Create empty Media record (with media folder so it
            #    lands in the right Admin UI section + inherits
            #    thumbnail config).
            media_id = uuid.uuid4().hex
            media_row: dict = {"id": media_id}
            if media_folder_id:
                media_row["mediaFolderId"] = media_folder_id
            try:
                client.request_post("_action/sync", payload={
                    "media": {
                        "entity": "media",
                        "action": "upsert",
                        "payload": [media_row],
                    },
                })
            except Exception:  # noqa: BLE001
                counters["failed"] += 1
                continue

            # 2. Trigger Shopware to fetch the URL into that media slot.
            #    File-name + extension are query params; Shopware
            #    persists them as the storage filename.
            tail = url.rsplit("/", 1)[-1]
            if "." in tail:
                stem, ext = tail.rsplit(".", 1)
                ext = ext.split("?")[0].lower() or "jpg"
            else:
                stem, ext = tail or "image", "jpg"
            uploaded_ok = False
            try:
                client.request_post(
                    f"_action/media/{media_id}/upload",
                    payload={"url": url},
                    additional_query_params={
                        "fileName": stem,
                        "extension": ext,
                    },
                )
                uploaded_ok = True
            except Exception as exc:  # noqa: BLE001
                # Bytes fallback — required when Shopware can't fetch
                # the URL itself (private host, ``CONTENT__MEDIA_ILLEGAL_URL``
                # from its url-allowlist, slow upstream, etc.). The
                # operator opts in via
                # ``Ecommerce Product Sync.image_strategy = 'url_with_bytes_fallback'``;
                # we treat that as "any error from the URL-fetch path
                # → try sending the bytes directly". A pure URL-only
                # strategy lets uploads silently fail with the failure
                # counter incrementing while the run reports overall
                # success — products end up with ``media=0`` and the
                # operator only spots it on storefront inspection.
                msg = str(exc)
                try:
                    file_bytes, mime_type = _fetch_local_file_bytes(url)
                except Exception:  # noqa: BLE001
                    file_bytes, mime_type = (None, None)
                if file_bytes:
                    try:
                        client.request_post(
                            f"_action/media/{media_id}/upload",
                            payload=file_bytes,
                            content_type=mime_type or "image/jpeg",
                            additional_query_params={
                                "fileName": stem,
                                "extension": ext,
                            },
                        )
                        uploaded_ok = True
                    except Exception as exc2:  # noqa: BLE001
                        try:
                            import frappe as _f
                            _f.logger("product_sync.shopware").warning(
                                f"image bytes-upload failed for "
                                f"{external_id} {stem}.{ext}: "
                                f"url-err={msg[:120]} bytes-err={str(exc2)[:120]}"
                            )
                        except Exception:  # noqa: BLE001
                            pass
                else:
                    try:
                        import frappe as _f
                        _f.logger("product_sync.shopware").warning(
                            f"image url-upload failed and bytes unavailable "
                            f"for {external_id} {stem}.{ext}: {msg[:200]}"
                        )
                    except Exception:  # noqa: BLE001
                        pass

            if uploaded_ok:
                counters["uploaded"] += 1
                new_media_ids.append(media_id)
                erp_basename = (
                    url.rstrip("/").rsplit("/", 1)[-1]
                    .split("?", 1)[0].lower()
                )
                if erp_basename:
                    counters["media_map"][erp_basename] = media_id
            else:
                counters["failed"] += 1

        # 3. Link new media records to the product. We append rather
        #    than replace — Shopware's POST semantics on product_media
        #    are upsert-by-(productId,mediaId). Supply deterministic
        #    ``id`` on each row so we have something to point at when
        #    we patch ``product.coverId`` below.
        if new_media_ids:
            import hashlib as _hl
            pm_rows = [
                {
                    "id": _hl.md5(f"pm::{external_id}::{mid}".encode()).hexdigest(),
                    "productId": external_id,
                    "mediaId": mid,
                    "position": idx,
                }
                for idx, mid in enumerate(new_media_ids)
            ]
            try:
                client.request_post("_action/sync", payload={
                    "pm": {
                        "entity": "product_media",
                        "action": "upsert",
                        "payload": pm_rows,
                    },
                })
            except Exception:  # noqa: BLE001
                # Best-effort link — media still exist and can be linked
                # manually by the operator if this fails.
                pm_rows = []

            # 4. Set the first product_media as cover if the product
            #    doesn't already have one. Without ``coverId`` set, the
            #    Store API returns ``cover: null`` and the storefront
            #    falls back to a placeholder — uploaded media exist but
            #    don't display. We check current cover first so a
            #    re-sync doesn't churn an operator's manual cover pick.
            if pm_rows:
                try:
                    cur = client.request_post("search/product", payload={
                        "filter": [{"type": "equals", "field": "id", "value": external_id}],
                        "includes": {"product": ["coverId"]},
                        "limit": 1,
                    })
                    has_cover = bool(
                        ((cur.get("data") or [{}])[0]).get("coverId")
                    )
                except Exception:  # noqa: BLE001
                    has_cover = False
                if not has_cover:
                    try:
                        client.request_patch(
                            f"product/{external_id}",
                            payload={"coverId": pm_rows[0]["id"]},
                        )
                    except Exception:  # noqa: BLE001
                        pass

        return counters

    def _reconcile_media_impl(
        self,
        client,
        *,
        external_id: str,
        keep_media_ids: set[str],
    ) -> dict:
        counters = {"deleted": 0, "kept": 0, "failed": 0, "cover_set": 0, "folder_set": 0}
        if not external_id:
            return counters

        # Pull live media + current cover + each media's folder so we
        # can (a) repoint cover to a survivor when the prior cover gets
        # deleted, (b) ensure a cover exists at all (re-syncs where
        # uploads were short-circuited never reached the cover-set
        # branch in ``_upload_images_impl``), and (c) move media into
        # the Product Media folder when they were created without one
        # (older sync runs predate the folder fix).
        r = client.request_post("search/product", payload={
            "filter": [{"type": "equals", "field": "id", "value": external_id}],
            "associations": {
                "media": {"associations": {"media": {}}},
            },
            "includes": {
                "product": ["coverId", "media"],
                "product_media": ["id", "mediaId", "media"],
                "media": ["id", "mediaFolderId"],
            },
            "limit": 1,
        })
        product = ((r.get("data") or [{}])[0]) or {}
        cur_cover = product.get("coverId")
        live_pm = product.get("media") or []

        to_delete: list[str] = []
        kept_pm: list[str] = []
        media_missing_folder: list[str] = []
        for pm in live_pm:
            m = pm.get("media") or {}
            mid = m.get("id")
            pm_id = pm.get("id")
            if not pm_id or not mid:
                continue
            if mid in keep_media_ids:
                kept_pm.append(pm_id)
                if not m.get("mediaFolderId"):
                    media_missing_folder.append(mid)
            else:
                to_delete.append(pm_id)

        counters["kept"] = len(kept_pm)

        if to_delete:
            try:
                client.request_post("_action/sync", payload={
                    "del": {
                        "entity": "product_media",
                        "action": "delete",
                        "payload": [{"id": pmid} for pmid in to_delete],
                    },
                })
                counters["deleted"] = len(to_delete)
            except Exception:  # noqa: BLE001
                counters["failed"] = len(to_delete)

        # Cover repair / ensure: re-point coverId if the prior cover
        # lived on a deleted row OR if no cover was ever set. Without
        # this Shopware returns the placeholder image (Store API
        # ``cover: null``) and the storefront falls back to a generic
        # image even though product_media rows exist.
        if kept_pm and (cur_cover in to_delete or not cur_cover):
            try:
                client.request_patch(
                    f"product/{external_id}",
                    payload={"coverId": kept_pm[0]},
                )
                counters["cover_set"] = 1
            except Exception:  # noqa: BLE001
                pass

        # Folder retrofit: media records created by older sync runs
        # have ``mediaFolderId=None`` and land in Shopware's
        # "Unsorted" bucket — which suppresses thumbnail generation
        # for that product. Move them into the Product Media folder.
        if media_missing_folder:
            folder_id = _resolve_product_media_folder_id(client)
            if folder_id:
                try:
                    client.request_post("_action/sync", payload={
                        "mv": {
                            "entity": "media",
                            "action": "upsert",
                            "payload": [
                                {"id": mid, "mediaFolderId": folder_id}
                                for mid in media_missing_folder
                            ],
                        },
                    })
                    counters["folder_set"] = len(media_missing_folder)
                except Exception:  # noqa: BLE001
                    pass

        return counters

    def _push_variant_options_impl(
        self,
        client,
        *,
        external_id: str,
        attribute_values: list[dict],
    ) -> dict:
        from ecommerce_integrations.shopware6.export.property_handler import (
            get_or_create_property_group,
            get_or_create_variant_option,
        )

        counters = {
            "set": 0, "failed": 0, "skipped": 0, "option_ids": [],
        }
        if not external_id or not attribute_values:
            return counters

        option_ids: list[str] = []
        for av in attribute_values:
            name = (av.get("name") or "").strip()
            value = av.get("value")
            value = str(value).strip() if value is not None else ""
            if not name or not value:
                counters["skipped"] += 1
                continue
            try:
                group_id = get_or_create_property_group(client, name)
                if not group_id:
                    counters["failed"] += 1
                    continue
                opt_id = get_or_create_variant_option(
                    client, group_id, name, value,
                )
                if not opt_id:
                    counters["failed"] += 1
                    continue
                option_ids.append(opt_id)
            except Exception:  # noqa: BLE001
                counters["failed"] += 1

        if option_ids:
            try:
                client.request_patch(
                    f"product/{external_id}",
                    payload={"options": [{"id": oid} for oid in option_ids]},
                )
                counters["set"] = len(option_ids)
            except Exception:  # noqa: BLE001
                counters["failed"] += len(option_ids)

        counters["option_ids"] = option_ids
        return counters

    def _push_template_configurator_impl(
        self,
        client,
        *,
        external_id: str,
        option_ids: list[str],
    ) -> dict:
        import hashlib as _hl
        counters = {"set": 0, "failed": 0}
        if not external_id or not option_ids:
            return counters
        rows = [
            {
                "id": _hl.md5(f"cs::{external_id}::{oid}".encode()).hexdigest(),
                "productId": external_id,
                "optionId": oid,
            }
            for oid in option_ids
        ]
        try:
            client.request_post("_action/sync", payload={
                "cs": {
                    "entity": "product_configurator_setting",
                    "action": "upsert",
                    "payload": rows,
                },
            })
            counters["set"] = len(rows)
        except Exception:  # noqa: BLE001
            counters["failed"] = len(rows)
        return counters

    def _push_properties_impl(
        self,
        client,
        *,
        external_id: str,
        properties: list[dict],
    ) -> dict:
        """Inner property-sync — runs under ``temp_shopware_session``.

        Reuses the existing ``shopware6.export.property_handler``
        helpers (``get_or_create_property_group`` /
        ``get_or_create_property_option``) so we share their cache +
        idempotent-by-id semantics. After resolving all option ids,
        we PATCH the product with ``properties: [{id: opt}, ...]``
        which Shopware treats as a full set-replace on the m2m link.

        Returns ``{linked, failed, skipped}``. Failures inside the
        helpers are swallowed (they log + return None); we only count
        the option-resolution and the final PATCH outcomes.
        """
        from ecommerce_integrations.shopware6.export.property_handler import (
            get_or_create_property_group,
            get_or_create_property_option,
        )

        counters = {"linked": 0, "failed": 0, "skipped": 0}
        if not external_id or not properties:
            return counters

        option_ids: list[str] = []
        for p in properties:
            name = (p.get("name") or "").strip()
            value = p.get("value")
            value = str(value).strip() if value is not None else ""
            if not name or not value:
                counters["skipped"] += 1
                continue
            try:
                group_id = get_or_create_property_group(client, name)
                if not group_id:
                    counters["failed"] += 1
                    continue
                opt_id = get_or_create_property_option(
                    client, group_id, name, value,
                )
                if not opt_id:
                    counters["failed"] += 1
                    continue
                option_ids.append(opt_id)
            except Exception:  # noqa: BLE001
                counters["failed"] += 1

        if option_ids:
            # ``properties`` is the m2m to property_group_option. PATCH
            # with the full id list = full replace, which is what we
            # want — the canonical Item row is the source of truth.
            try:
                client.request_patch(
                    f"product/{external_id}",
                    payload={"properties": [{"id": oid} for oid in option_ids]},
                )
                counters["linked"] = len(option_ids)
            except Exception:  # noqa: BLE001
                counters["failed"] += len(option_ids)

        return counters

    def _deactivate_impl(self, client, external_id: str) -> None:
        sync_payload = {
            "products": {
                "entity": "product",
                "action": "upsert",
                "payload": [{"id": external_id, "active": False}],
            },
        }
        try:
            client.request_post("_action/sync", payload=sync_payload)
        except Exception as e:
            raise AdapterError(
                f"Shopware product deactivate failed: {e}",
            ) from e

    def _delete_impl(self, client, external_id: str) -> None:
        sync_payload = {
            "products": {
                "entity": "product",
                "action": "delete",
                "payload": [{"id": external_id}],
            },
        }
        try:
            client.request_post("_action/sync", payload=sync_payload)
        except Exception as e:
            # 404 = already gone; idempotent per the ABC contract.
            if "404" in str(e):
                return
            raise AdapterError(
                f"Shopware product delete failed: {e}",
            ) from e

    def _find_matching_impl(
        self,
        client,
        *,
        sku: str | None,
        ean: str | None,
        name: str | None,
    ) -> list[ProductMatch]:
        if not (sku or ean or name):
            return []

        matches: list[ProductMatch] = []
        seen: set[str] = set()

        # SKU is the strongest signal: 1.0. Exact match on
        # ``productNumber`` (Shopware's name for SKU).
        if sku:
            for m in self._run_match_query(
                client,
                field="productNumber",
                value=sku,
                score=1.0,
            ):
                if m.external_id not in seen:
                    seen.add(m.external_id)
                    matches.append(m)

        # EAN second: 0.8. Different products legitimately share an EAN
        # (re-issued barcodes, multi-pack vs single) so we soft-rank
        # below the SKU hit.
        if ean:
            for m in self._run_match_query(
                client,
                field="ean",
                value=ean,
                score=0.8,
            ):
                if m.external_id not in seen:
                    seen.add(m.external_id)
                    matches.append(m)

        # Name last: 0.5. Exact match only (fuzzy would invite
        # adoption mistakes — operator clicks "match" on the wrong
        # item, ERP and backend are now welded together permanently).
        if name:
            for m in self._run_match_query(
                client,
                field="name",
                value=name,
                score=0.5,
            ):
                if m.external_id not in seen:
                    seen.add(m.external_id)
                    matches.append(m)

        # Already cap-limited per query; sort by score desc for the UI.
        matches.sort(key=lambda mm: mm.score, reverse=True)
        return matches[:_MATCH_LIMIT]

    def _find_matching_bulk_impl(
        self,
        client,
        *,
        skus: list[str] | None,
        eans: list[str] | None,
    ) -> dict[str, ProductMatch]:
        # Batched ``equalsAny`` lookup. Shopware's filter limit on
        # ``equalsAny`` lists is generous (well into the thousands), but
        # we chunk at the same page size we use for fetch so we don't
        # have one outlier endpoint defining the API ergonomics.
        out: dict[str, ProductMatch] = {}

        def _batch(field: str, values: list[str], score: float) -> None:
            # Dedupe + drop blanks before chunking so we don't waste a
            # whole page on noise.
            seen: set[str] = set()
            cleaned: list[str] = []
            for v in values:
                v = (v or "").strip()
                if v and v not in seen:
                    seen.add(v)
                    cleaned.append(v)
            for i in range(0, len(cleaned), _FETCH_PAGE_SIZE):
                chunk = cleaned[i : i + _FETCH_PAGE_SIZE]
                criteria = {
                    "limit": _FETCH_PAGE_SIZE,
                    "filter": [
                        {"type": "equalsAny", "field": field, "value": chunk},
                    ],
                    "includes": {
                        "product": ["id", "productNumber", "ean", "name"],
                    },
                }
                try:
                    resp = client.request_post(
                        "search/product", payload=criteria,
                    )
                except Exception as e:
                    raise AdapterError(
                        f"Shopware bulk match search failed "
                        f"({field}, chunk size {len(chunk)}): {e}",
                    ) from e
                for row in (resp or {}).get("data") or []:
                    attrs = row.get("attributes") or {}
                    merged = {
                        **attrs,
                        **{k: v for k, v in row.items() if k != "attributes"},
                    }
                    ext_id = merged.get("id")
                    if not ext_id:
                        continue
                    key = merged.get(field)
                    if not key:
                        continue
                    # SKU wins over EAN if both lookups happen to hit
                    # the same product through different keys.
                    if key in out and field == "ean":
                        continue
                    out[str(key)] = ProductMatch(
                        external_id=ext_id,
                        sku=merged.get("productNumber") or None,
                        name=merged.get("name") or "",
                        score=score,
                    )

        if skus:
            _batch("productNumber", skus, score=1.0)
        if eans:
            _batch("ean", eans, score=0.8)
        return out

    def _run_match_query(
        self,
        client,
        *,
        field: str,
        value: str,
        score: float,
    ) -> list[ProductMatch]:
        criteria = {
            "limit": _MATCH_LIMIT,
            "filter": [
                {"type": "equals", "field": field, "value": value},
            ],
            "includes": {
                "product": ["id", "productNumber", "name"],
            },
        }
        try:
            resp = client.request_post(
                "search/product", payload=criteria,
            )
        except Exception as e:
            raise AdapterError(
                f"Shopware product match search failed ({field}={value!r}): {e}",
            ) from e

        out: list[ProductMatch] = []
        for row in (resp or {}).get("data") or []:
            attrs = row.get("attributes") or {}
            merged = {**attrs, **{k: v for k, v in row.items() if k != "attributes"}}
            ext_id = merged.get("id")
            if not ext_id:
                continue
            out.append(
                ProductMatch(
                    external_id=ext_id,
                    sku=merged.get("productNumber") or None,
                    name=merged.get("name") or "",
                    score=score,
                ),
            )
        return out

    # ─── Live-hash payload builders ──────────────────────────────────

    def _build_live_canonical(
        self, product: dict[str, Any], sync,
    ) -> dict[str, Any]:
        """Build a canonical-payload dict from a live Shopware product.

        Section names and shapes mirror
        :func:`product_sync.engine.canonical.build_canonical_payload`
        exactly. Toggles are honoured via ``_live_flag`` — the same
        ``sync_*`` flags that gate the ERP-side sections gate the
        live-side ones, otherwise the differ would compare a
        toggle-trimmed ERP payload against a full live payload and
        always declare drift.
        """
        merged = self._merge_attrs(product)
        out: dict[str, Any] = {
            "v": PAYLOAD_VERSION,
            "item_code": _ns(merged.get("productNumber")),
            # ``variant_of`` deliberately not emitted — ERPNext uses
            # the parent item_code, Shopware uses its own parentId
            # UUID. Hashing either side would force permanent drift.
            "is_variant": bool(merged.get("parentId")),
        }
        if _live_flag(sync, "sync_basic_fields", default=True):
            out["basic"] = self._live_basic(merged)
        if _live_flag(sync, "sync_pricing", default=True):
            out["pricing"] = self._live_pricing(merged, sync)
        if _live_flag(sync, "sync_inventory", default=True):
            out["inventory"] = self._live_inventory(merged)
        if _live_flag(sync, "sync_images", default=True):
            out["images"] = self._live_images(merged)
        if _live_flag(sync, "sync_properties", default=True):
            out["properties"] = self._live_properties(merged)
        if _live_flag(sync, "sync_seo_fields", default=False):
            out["seo"] = self._live_seo(merged)
        if _live_flag(sync, "sync_taxes", default=True):
            out["taxes"] = self._live_taxes(merged, sync)
        out["categories"] = self._live_categories(merged)
        return out

    def _merge_attrs(self, product: dict[str, Any]) -> dict[str, Any]:
        attrs = product.get("attributes") or {}
        return {**attrs, **{k: v for k, v in product.items() if k != "attributes"}}

    def _live_basic(self, merged: dict[str, Any]) -> dict[str, Any]:
        translated = merged.get("translated") or {}
        return {
            "name": _ns(merged.get("name") or translated.get("name")),
            "sku": _ns(merged.get("productNumber")),
            "description": _ns(
                merged.get("description") or translated.get("description"),
            ),
            "ean": _ns(merged.get("ean")),
            "is_active": bool(merged.get("active", True)),
            # Shopware stores the UoM on a separate ``unit`` association;
            # without expanding it we can only echo the unit-id. The ERP
            # side hashes the UoM name, so a UoM-only drift may not
            # converge until Phase 5 expands the unit association.
            # TODO: expand ``unit`` association to compare on UoM name.
            "uom": _ns(merged.get("unitId")),
        }

    def _live_pricing(
        self, merged: dict[str, Any], sync,
    ) -> dict[str, Any]:
        prices = merged.get("price") or []
        base = 0.0
        currency_iso = ""
        if isinstance(prices, list) and prices:
            first = prices[0] or {}
            try:
                # Always read gross so we match the canonical's basis.
                # 2-decimal precision (currency cents) to keep hash
                # parity with the canonical — sub-cent rounding from
                # net-to-gross computation would otherwise flap.
                base = round(float(first.get("gross") or first.get("net") or 0), 2)
            except (TypeError, ValueError):
                base = 0.0
            # Resolve the Shopware currency UUID to an ISO code so we
            # hash on the same shape the canonical emits ("EUR", not a
            # UUID). The mapping is reverse-cached per session to keep
            # the per-item cost negligible.
            currency_id = _ns(first.get("currencyId"))
            if currency_id:
                currency_iso = _resolve_iso_for_currency_id(currency_id) or ""

        # Tax rate from the product's ``tax`` association (Shopware
        # returns ``{taxRate: 19.0, ...}``). Falls back to 19 % so a
        # missing association doesn't force a spurious hash mismatch
        # on installs where Shopware didn't include the association.
        tax_rate_pct = 19.0
        tax = merged.get("tax")
        if isinstance(tax, dict):
            raw = tax.get("taxRate")
            if raw is not None:
                try:
                    tax_rate_pct = float(raw)
                except (TypeError, ValueError):
                    pass

        # Channel-tier prices live in the ``prices`` association
        # (rule-based price tiers) — Phase 5 will reconcile these
        # against ERP price lists. For now we emit an empty list, which
        # matches what ``_canonical_pricing`` does on the ERP side when
        # the sync has no per-channel overrides configured.
        return {
            "currency": currency_iso,
            "base_price": base,
            "channel_prices": [],
            "tax_rate_pct": round(tax_rate_pct, 4),
        }

    def _live_inventory(self, merged: dict[str, Any]) -> dict[str, Any]:
        # ERP side sums across warehouses; live side reports the same
        # number Shopware shows the customer — see _row_to_live_node
        # for the availableStock-over-stock preference.
        raw = merged.get("availableStock")
        if raw is None:
            raw = merged.get("stock")
        try:
            qty = round(float(raw or 0), 4)
        except (TypeError, ValueError):
            qty = 0.0
        return {"qty": qty}

    def _live_images(self, merged: dict[str, Any]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        cover = merged.get("cover")
        cover_id = cover.get("mediaId") if isinstance(cover, dict) else None
        for m in merged.get("media") or []:
            if not isinstance(m, dict):
                continue
            inner = m.get("media") or {}
            url = inner.get("url") if isinstance(inner, dict) else None
            media_id = (inner.get("id") if isinstance(inner, dict) else None) or m.get("mediaId")
            if not url:
                continue
            out.append({
                "url": url,
                "primary": bool(cover_id and media_id == cover_id),
            })
        out.sort(key=lambda d: d["url"])
        return out

    def _live_properties(self, merged: dict[str, Any]) -> dict[str, Any]:
        # Brand lives on the expanded ``manufacturer`` association,
        # not in the ``properties`` array. The fetch criteria above
        # explicitly includes ``manufacturer`` so the name is reachable
        # here; without it brand would be "" and 100 % of items would
        # report a brand-drift on every preview.
        attrs_list: list[dict[str, str]] = []
        for prop in merged.get("properties") or []:
            if not isinstance(prop, dict):
                continue
            group = prop.get("group") or {}
            group_name = (
                group.get("name") if isinstance(group, dict) else None
            )
            value = prop.get("name") or prop.get("translated", {}).get("name")
            if group_name and value:
                attrs_list.append({
                    "name": _ns(group_name),
                    "value": _ns(value),
                })
        attrs_list.sort(key=lambda d: (d["name"], d["value"]))

        # Pull brand from ``manufacturer.name`` (or translated.name as a
        # fallback for non-default locales). Shopware's ``manufacturer``
        # association maps to ERPNext's ``Item.brand`` — Shopware has no
        # separate Brand concept, so the manufacturer is the brand.
        # ``properties.manufacturer`` stays empty to match what the
        # canonical builder emits from ``Item.manufacturer`` (a
        # separate, usually-empty Item field).
        mfr = merged.get("manufacturer") or {}
        brand_name = ""
        if isinstance(mfr, dict):
            brand_name = (
                mfr.get("name")
                or (mfr.get("translated") or {}).get("name")
                or ""
            )

        return {
            "brand": _ns(brand_name),
            "manufacturer": "",
            "attributes": attrs_list,
            # Shopware ``properties`` are the ecommerce_properties from
            # ERPNext.  Sorted identically to the canonical builder so
            # the hash comparison picks up adds/removes/value changes.
            "ecommerce_properties": attrs_list,
        }

    def _live_seo(self, merged: dict[str, Any]) -> dict[str, Any]:
        return {
            # ``seo_url`` lives on a separate entity; not expanded here.
            # Phase 5 picks this up once the export side actually ships
            # slugs.
            "slug": "",
            "meta_title": _ns(merged.get("metaTitle")),
            "meta_description": _ns(merged.get("metaDescription")),
        }

    def _live_taxes(
        self, merged: dict[str, Any], sync,
    ) -> dict[str, Any]:
        """Tax rate (%) Shopware reports via the ``tax`` association.

        Hashes the same shape as ``_canonical_taxes``: a single
        ``rate_pct`` float, normalised to 4 decimals. Mismatched
        backend tax-entity names that used to force permanent drift
        are gone — the percentage is the only anchor that survives
        the round-trip cleanly.
        """
        rate = 0.0
        tax = merged.get("tax") or {}
        if isinstance(tax, dict):
            raw = tax.get("taxRate")
            if raw is not None:
                try:
                    rate = float(raw)
                except (TypeError, ValueError):
                    rate = 0.0
        return {"rate_pct": round(rate, 4)}

    def _live_categories(self, merged: dict[str, Any]) -> dict[str, Any]:
        """Live category UUIDs Shopware reports for this product.

        Mirrors the shape ``_canonical_categories`` produces on the ERP
        side: ``{"item_group": <name-or-empty>, "ids": [<uuid>, …]}``.
        The ``ids`` list is what we actually hash for parity. ``item_group``
        stays empty here because Shopware doesn't carry the ERP-side IG
        name — only its own category UUIDs.
        """
        ids: list[str] = []
        for c in merged.get("categories") or []:
            if isinstance(c, dict) and c.get("id"):
                ids.append(c["id"])
        if not ids:
            for raw in merged.get("categoryIds") or []:
                if raw:
                    ids.append(str(raw))
        # Match canonical's shape: only the UUID set goes into the hash.
        return {"ids": sorted(set(ids))}


# ─── Module-level helpers ────────────────────────────────────────────


_PRODUCT_MEDIA_FOLDER_ID_CACHE: dict[str, str | None] = {}


def _resolve_product_media_folder_id(client) -> str | None:
    """Return the Shopware ``media_folder.id`` for the product-media bucket.

    Shopware ships a default folder named "Product Media" linked to the
    ``product`` entity definition (visible in Admin → Media → Files);
    new product images should land there so they inherit the folder's
    thumbnail config and admin-UI grouping. Without ``mediaFolderId``
    on the create, the media entity lands in the unsorted root bucket.

    Lookup strategy (first hit wins, cached per-process):

    1. Folder whose ``defaultFolder.entity == 'product'`` — the
       canonical link Shopware itself uses for product uploads.
    2. Folder named ``"Product Media"`` (or its translated alias) as
       a fallback for installations where the default folder was
       renamed.
    3. ``None`` — caller skips the ``mediaFolderId`` field, matching
       the legacy behaviour so we stay backwards-compatible.
    """
    base_id = id(client)  # different client → different cache slot
    key = f"client::{base_id}"
    if key in _PRODUCT_MEDIA_FOLDER_ID_CACHE:
        return _PRODUCT_MEDIA_FOLDER_ID_CACHE[key]

    folder_id: str | None = None
    try:
        # Direct filter on the to-one association — Shopware indexes
        # ``media_folder.default_folder.entity`` so this is a single
        # row lookup, not a tablescan. Stable across Shopware versions
        # (the relation has been in place since 6.0) and storefront
        # locales (the filter targets the entity-key, not the
        # translated folder name).
        resp = client.request_post("search/media-folder", payload={
            "filter": [{
                "type": "equals",
                "field": "defaultFolder.entity",
                "value": "product",
            }],
            "limit": 1,
        })
        rows = resp.get("data") or []
        if rows:
            folder_id = rows[0].get("id")
        if not folder_id:
            # Fallback: scan media-default-folder ourselves in case the
            # joined filter has been disabled on this install. Returns
            # the same id by a different path.
            ddf = client.request_post("search/media-default-folder", payload={
                "filter": [{"type": "equals", "field": "entity", "value": "product"}],
                "associations": {"folder": {}},
                "limit": 1,
            })
            ddf_rows = ddf.get("data") or []
            if ddf_rows:
                folder = ddf_rows[0].get("folder") or {}
                folder_id = folder.get("id") if isinstance(folder, dict) else None
    except Exception:  # noqa: BLE001
        folder_id = None

    _PRODUCT_MEDIA_FOLDER_ID_CACHE[key] = folder_id
    return folder_id


def _fetch_local_file_bytes(url: str) -> tuple[bytes | None, str | None]:
    """Read the raw bytes of a Frappe-served file referenced by ``url``.

    Used by the bytes-fallback path of the image uploader when Shopware
    rejects the URL (private host, ``CONTENT__MEDIA_ILLEGAL_URL``,
    timeout, …). Reads from the local File DocType (private +
    public + DFP External Storage cases all surface through
    ``frappe.utils.file_manager.get_file``), so we avoid an extra
    HTTP roundtrip back to ourselves.

    Returns ``(bytes, mime_type)`` on success, ``(None, None)`` if
    the URL doesn't map to a Frappe File the orchestrator can read.
    """
    if not url:
        return (None, None)
    # Extract the file_url portion (everything from ``/files/`` or
    # ``/private/files/`` onward, or the ``/file/<hash>/<name>`` shape
    # the ERPNext custom router emits).
    import re as _re
    import mimetypes as _mt
    path = ""
    m = _re.search(r"(/files/.*|/private/files/.*|/file/[^/]+/[^/?#]+)", url)
    if m:
        path = m.group(1)
    else:
        return (None, None)

    try:
        import frappe as _f
        # Prefer the canonical lookup chain. ``get_file_data_from_hash``
        # handles ``/file/<hash>/<name>`` and ``/files/<name>``.
        file_doc = None
        if path.startswith("/file/"):
            hash_part = path.split("/", 3)[2]
            row = _f.db.get_value(
                "File", {"file_url": ("like", f"%/{hash_part}/%")},
                ["name", "file_url"], as_dict=True,
            )
            if row:
                file_doc = _f.get_doc("File", row["name"])
        if file_doc is None:
            row = _f.db.get_value(
                "File", {"file_url": path},
                ["name"], as_dict=True,
            )
            if row:
                file_doc = _f.get_doc("File", row["name"])
        if file_doc is None:
            return (None, None)
        content = file_doc.get_content()
        if isinstance(content, str):
            content = content.encode("utf-8")
        mime, _ = _mt.guess_type(file_doc.file_name or "")
        return (content, mime or "application/octet-stream")
    except Exception:  # noqa: BLE001
        return (None, None)


def property_option_uuid(group_name: str, value: str, kind: str = "property") -> str:
    """Deterministic UUID for a Shopware ``property_group_option`` row,
    matching the scheme that ``shopware6.export.property_handler``
    used for in-place creation. ``kind="property"`` matches
    ``get_or_create_property_option`` (filterable properties);
    ``kind="variant"`` matches ``get_or_create_variant_option`` (variant
    configurator). The two intentionally produce different UUIDs so the
    same value can exist as both a property and a variant option."""
    from ecommerce_integrations.shopware6.export.utils import generate_uuid
    prefix = "variant_option" if kind == "variant" else "property_option"
    return generate_uuid(f"{prefix}_{group_name}_{value}")


# Process-local "ambient" Shopware client. When set, ``_with_client``
# reuses it instead of running ``temp_shopware_session`` (which
# allocates a fresh ``Shopware6AdminAPIClientBase`` and does an OAuth
# handshake every time). The pattern lets one outer session amortise
# its handshake across many nested ``adapter.X(...)`` calls — the
# difference between ~1 OAuth roundtrip per *call* and ~1 per *job*
# on the apply hot path.
_AMBIENT_CLIENT: list = [None]


class shared_shopware_session:
    """Context manager that pins one Shopware client for the duration
    of a block. ``_with_client`` calls inside the ``with`` reuse the
    same authenticated client instead of opening a fresh session each
    time.

    Use sparingly — the right scope is a tight loop that calls several
    adapter helpers per item. Outside of that the per-call session
    semantics (fresh idempotency key, retry budget) are what you want.
    """

    def __init__(self):
        self._client = None
        self._previous = None

    def __enter__(self):
        from ecommerce_integrations.shopware6.connection import (
            get_shopware_client,
        )
        self._client = get_shopware_client()
        self._previous = _AMBIENT_CLIENT[0]
        _AMBIENT_CLIENT[0] = self._client
        return self._client

    def __exit__(self, exc_type, exc, tb):
        _AMBIENT_CLIENT[0] = self._previous
        client = self._client
        self._client = None
        if client is not None:
            session = getattr(client, "session", None)
            if session is not None:
                try:
                    session.close()
                except Exception:  # noqa: BLE001
                    pass


def _with_client(fn, *args, **kwargs):
    """Run ``fn`` inside a Shopware admin session.

    Reuses the process-local ``_AMBIENT_CLIENT`` when an outer
    ``shared_shopware_session`` context is active; otherwise falls
    back to a fresh per-call session via ``temp_shopware_session``.
    In test mode (``frappe.flags.in_test``) the decorator path runs
    ``fn`` without the client so tests can pass a mock.
    """
    ambient = _AMBIENT_CLIENT[0]
    if ambient is not None and not getattr(frappe.flags, "in_test", False):
        # Rotate the idempotency key per call so Shopware doesn't
        # dedupe distinct writes as retries of the first one. Without
        # this every call on the shared client would carry whatever
        # key was attached when the session opened (or none at all),
        # and Shopware's idempotency cache would either reject the
        # second write as a key/payload mismatch or — worse — return
        # the cached first response. The rotator patches the client
        # once and just swaps the key field on subsequent calls.
        import uuid as _u
        from ecommerce_integrations.shopware6.connection import (
            _ensure_idempotency_rotator,
        )
        _ensure_idempotency_rotator(ambient).key = _u.uuid4().hex
        return fn(ambient, *args, **kwargs)

    @temp_shopware_session
    def runner(client, *inner_args, **inner_kwargs):
        return fn(client, *inner_args, **inner_kwargs)

    return runner(*args, **kwargs)


def _resolve_iso_for_currency_id(currency_id: str) -> str | None:
    """Reverse of ``payload._resolve_shopware_currency_id``.

    Map a Shopware ``currency.id`` UUID back to the ISO code so the
    live-side canonical can hash the same shape the ERP-side emits.
    Cached per-host to keep the per-item cost negligible during a
    DETAIL preview's drift fetch.
    """
    if not currency_id:
        return None
    try:
        import frappe
        cache_key = f"_psync_shopware_currency_iso:{currency_id}"
        cached = frappe.cache().get_value(cache_key)
        if cached:
            return cached or None

        @temp_shopware_session
        def _lookup(client):
            resp = client.request_post(
                "search/currency",
                payload={
                    "limit": 1,
                    "filter": [{"type": "equals", "field": "id", "value": currency_id}],
                    "includes": {"currency": ["id", "isoCode"]},
                },
            )
            data = (resp or {}).get("data") or []
            if data:
                attrs = (data[0].get("attributes") or {})
                return (data[0].get("isoCode") or attrs.get("isoCode") or "").upper() or None
            return None

        value = _lookup()
        if value:
            frappe.cache().set_value(cache_key, value, expires_in_sec=3600)
        return value
    except Exception:  # noqa: BLE001
        return None


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
