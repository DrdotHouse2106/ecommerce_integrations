"""
Shopware 6 Base Infrastructure Module

Provides shared infrastructure components:
- CacheManager: Centralized cache management with TTL and invalidation
- ShopwareSettingController: Base class for Shopware settings
"""

from ecommerce_integrations.shopware6.base.cache_manager import ShopwareCacheManager
from ecommerce_integrations.shopware6.base.setting_controller import ShopwareSettingController

__all__ = [
    "ShopwareCacheManager",
    "ShopwareSettingController",
]
