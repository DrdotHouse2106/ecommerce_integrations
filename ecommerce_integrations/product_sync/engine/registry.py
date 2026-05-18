"""Registry of backend-specific :class:`ProductAdapter` instances.

Same lookup contract as :func:`catalog_mirror.engine.adapters.base.get_adapter`
(which puts the registry inside the base module). Keeping it as a
separate module here is intentional — product_sync's registry will
grow (adapter pools, rate-limit budgets, circuit-breaker state) and
deserves its own home. The orchestrator asks ``get_adapter(backend)``
and gets back an instance without having to know which module ships
it, so adding a third backend (Magento, BigCommerce, …) is one line.
"""

from __future__ import annotations

from ecommerce_integrations.product_sync.constants import (
    BACKEND_MEDUSA,
    BACKEND_SHOPWARE,
)
from ecommerce_integrations.product_sync.engine.adapters.base import (
    AdapterError,
    ProductAdapter,
)


def get_adapter(backend: str) -> ProductAdapter:
    """Return a fresh :class:`ProductAdapter` for ``backend``.

    The import is intentionally lazy: pulling in the Shopware adapter
    at module import time would drag in the shopware6 session helpers
    even on sites that only run Medusa, and vice versa. Importing only
    inside the function keeps the dependency graph clean.
    """
    from ecommerce_integrations.product_sync.engine.adapters.medusa import (
        MedusaProductAdapter,
    )
    from ecommerce_integrations.product_sync.engine.adapters.shopware import (
        ShopwareProductAdapter,
    )

    if backend == BACKEND_SHOPWARE:
        return ShopwareProductAdapter()
    if backend == BACKEND_MEDUSA:
        return MedusaProductAdapter()
    raise AdapterError(f"No ProductAdapter registered for backend: {backend}")
