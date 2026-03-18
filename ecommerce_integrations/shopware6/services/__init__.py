"""Shopware 6 service package.

Keep package initialization lightweight so submodule imports from
``ecommerce_integrations.shopware6.services.*`` do not trigger circular imports.
"""

__all__ = ["PriceService"]


def __getattr__(name):
    """Resolve heavyweight service exports lazily."""
    if name == "PriceService":
        from ecommerce_integrations.shopware6.services.price_service import PriceService

        return PriceService
    raise AttributeError(name)
