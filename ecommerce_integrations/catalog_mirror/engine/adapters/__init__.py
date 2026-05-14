"""CatalogAdapter implementations register themselves into ``base._REGISTRY``
at import time via the ``@register(backend)`` decorator. Import the
concrete adapter modules here so the registry is populated on package
import — otherwise ``tasks.apply_mirror`` calls ``get_adapter("Shopware")``
and gets ``No adapter registered for backend 'Shopware'`` because the
module that would have registered it was never imported.

The Medusa adapter is a stub for Phase 1 — concrete implementation
lands in Phase 5.
"""

from ecommerce_integrations.catalog_mirror.engine.adapters import (  # noqa: F401
    medusa,
    shopware,
)
