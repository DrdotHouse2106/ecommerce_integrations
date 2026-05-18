"""Backend-agnostic product adapter interface for Product Sync.

Each backend implements this interface and ships a concrete subclass
under ``product_sync.engine.adapters.<backend>``. The orchestrator
locates the right adapter via
:func:`product_sync.engine.registry.get_adapter` based on
``sync.backend``.

Adapters are responsible for:

- streaming live products under a filter set (paginated)
- locating an existing product by SKU/name for the Match workflow
- upserting a single product (create or patch)
- deactivating a product (soft-disable, not delete)
- deleting a product (hard delete; only on explicit orphan_policy=delete)

Errors are surfaced via :class:`AdapterError` so the orchestrator can
route them into ``Ecommerce Integration Log`` consistently. The
contract is intentionally per-product — the orchestrator owns the
ordering, batching and retry policy.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass, field


class AdapterError(Exception):
    """Raised by adapters for sync-time failures (HTTP errors, mapping
    misses, etc). The orchestrator catches and logs to ``Ecommerce
    Integration Log``; the original traceback is preserved via
    ``__cause__``."""


@dataclass
class LiveProductNode:
    """One backend product as it exists right now.

    Used by both the orphan-detection path (compare live IDs against
    the expected set) and the drift-detection path (compare per-field
    values against the proposed payload). ``images`` is a flat list of
    URLs; binary handling is the adapter's concern.
    """

    external_id: str
    sku: str | None
    name: str
    description: str | None = None
    price: float | None = None
    stock: int | None = None
    category_ids: list[str] = field(default_factory=list)
    sales_channel_ids: list[str] = field(default_factory=list)
    active: bool = True
    images: list[str] = field(default_factory=list)


@dataclass
class ProductMatch:
    """A candidate backend product for the Match workflow.

    Returned from :meth:`ProductAdapter.find_matching_products` when an
    Item has no recorded external_id but the operator suspects a
    same-SKU product already exists. ``score`` is adapter-defined
    (typically 0..1) so the UI can rank suggestions.
    """

    external_id: str
    sku: str | None
    name: str
    score: float


class ProductAdapter(ABC):
    """Abstract base for per-backend product adapters.

    Subclasses set ``backend`` to the canonical string identifier
    (``Shopware`` / ``Medusa`` / …) so the registry can route by name
    without inspecting the class.
    """

    backend: str = ""

    @abstractmethod
    def fetch_products(
        self,
        *,
        sales_channel_ids: list[str] | None = None,
        category_ids: list[str] | None = None,
        external_ids: list[str] | None = None,
    ) -> Iterator[LiveProductNode]:
        """Stream live products matching the supplied filter.

        All filter arguments are AND-combined: passing both
        ``sales_channel_ids`` and ``category_ids`` returns products in
        any of the listed channels AND any of the listed categories.
        ``None`` for any argument means "no constraint on that
        dimension". Implementations should paginate so the iterator
        works on stores with five-figure catalogues.
        """

    @abstractmethod
    def upsert_product(
        self,
        *,
        external_id: str | None,
        payload: dict,
        target_sales_channels: list[str],
    ) -> str:
        """Create or update a single product. Return its external_id.

        When ``external_id`` is ``None`` this creates a new product and
        returns the freshly minted id. When set, the call patches the
        existing product. ``target_sales_channels`` is honoured by
        backends that support per-channel visibility (Shopware);
        adapters for backends that don't (Medusa) should ignore it.
        Implementations must be idempotent on repeated calls with the
        same inputs.
        """

    def upsert_products_bulk(
        self,
        items: list[dict],
        *,
        target_sales_channels: list[str],
    ) -> list[dict]:
        """Upsert many products in one or more batched backend calls.

        Each entry in ``items`` is ``{external_id, payload}``. The
        return list mirrors the input order and each row is
        ``{external_id, error}`` — ``error`` is ``None`` on success.
        ``external_id`` is the canonical backend id (may differ from
        the input on first create).

        Default fallback: call :meth:`upsert_product` per item. Real
        adapters should override and batch on the backend's bulk
        endpoint (Shopware ``POST /_action/sync``, Medusa
        ``POST /admin/products/batch``) — that's the single biggest
        wall-time win for the apply loop on five-figure catalogues.
        """
        out: list[dict] = []
        for entry in items:
            try:
                ext_id = self.upsert_product(
                    external_id=entry.get("external_id"),
                    payload=entry.get("payload") or {},
                    target_sales_channels=target_sales_channels,
                )
                out.append({"external_id": ext_id, "error": None})
            except Exception as exc:  # noqa: BLE001
                out.append({
                    "external_id": entry.get("external_id"),
                    "error": str(exc),
                })
        return out

    @abstractmethod
    def deactivate_product(self, external_id: str) -> None:
        """Soft-disable the product so it's hidden from storefronts but
        not removed. Backends without an "active" flag simulate this by
        unlinking from every sales channel."""

    @abstractmethod
    def delete_product(self, external_id: str) -> None:
        """Hard-delete the product. Called only on ``orphan_policy=delete``
        or explicit per-row deletes from the preview dialog. 404 is
        acceptable (treated as already-deleted; idempotent)."""

    @abstractmethod
    def find_matching_products(
        self,
        *,
        sku: str | None = None,
        name: str | None = None,
    ) -> list[ProductMatch]:
        """Search the backend for products matching ``sku`` and/or ``name``.

        At least one of ``sku`` / ``name`` must be supplied. Adapters
        should cap results (25 is a reasonable default) and order by
        descending score so the UI can pick the top match without
        re-sorting.
        """

    def find_matching_products_bulk(
        self,
        *,
        skus: list[str] | None = None,
        eans: list[str] | None = None,
    ) -> dict[str, ProductMatch]:
        """Bulk variant of :meth:`find_matching_products` for adoption.

        Looks up many SKUs (and optionally EANs) in batched backend
        queries instead of one HTTP call per item — the difference
        between seconds and hours when adopting a five-figure catalogue.

        Returns a flat ``{lookup_value -> ProductMatch}`` mapping. The
        key is the SKU/EAN that produced the hit. SKU matches are
        preferred over EAN matches when both fire for the same backend
        product. Adapters that don't override this method fall back to
        N calls of :meth:`find_matching_products`; concrete adapters
        should override for real catalogue sizes.
        """
        result: dict[str, ProductMatch] = {}
        for sku in skus or []:
            try:
                matches = self.find_matching_products(sku=sku)
            except Exception:  # noqa: BLE001
                matches = []
            if len(matches) == 1:
                result[sku] = matches[0]
        for ean in eans or []:
            if ean in result:
                continue
            try:
                matches = self.find_matching_products(name=ean)
            except Exception:  # noqa: BLE001
                matches = []
            if len(matches) == 1:
                result[ean] = matches[0]
        return result
