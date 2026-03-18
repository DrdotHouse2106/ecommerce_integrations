"""
Shopware 6 Cache Manager

Centralized cache management replacing scattered global caches.
Provides TTL-based caching with Redis backend and automatic invalidation.

Replaces the following global caches from product_export.py:
- _property_group_cache
- _property_option_cache
- _category_cache
- _media_folder_cache
- _custom_field_set_cache
- _category_custom_field_set_cache
- _variant_option_cache
- Redis caches for currency, sales_channel, image_hash, field_mappings
"""

from typing import Any, Dict, Optional
import frappe
from ecommerce_integrations.shopware6.utils import require_shopware_admin


class ShopwareCacheManager:
    """
    Thread-safe, TTL-based cache manager for Shopware integration.

    Uses Frappe's Redis cache for persistence across requests.
    Provides a unified interface for all Shopware caching needs.

    Cache Types:
        - property_group: Shopware property group IDs
        - property_option: Shopware property option IDs
        - category: Shopware category IDs by ERPNext Item Group
        - media_folder: Shopware media folder IDs
        - custom_field_set: Shopware custom field set IDs
        - category_custom_field_set: Category custom field set ID
        - variant_option: Variant option IDs
        - currency: Currency IDs by ISO code
        - sales_channel: Default sales channel ID
        - tax: Tax IDs
        - field_mappings: Configured field mappings
        - image_hash: MD5 hashes for delta-sync

    Usage:
        cache = ShopwareCacheManager()

        # Get cached value
        currency_id = cache.get("currency", "EUR")

        # Set with TTL
        cache.set("currency", "EUR", currency_id, ttl=86400)

        # Invalidate specific key
        cache.invalidate("currency", "EUR")

        # Invalidate entire cache type
        cache.invalidate_all("currency")

        # Clear all caches
        cache.clear_all()
    """

    # Redis key prefix for all Shopware caches
    REDIS_PREFIX = "shopware6:cache:"

    # Default TTL values (in seconds)
    DEFAULT_TTL = {
        "property_group": 3600,      # 1 hour
        "property_option": 3600,     # 1 hour
        "category": 3600,            # 1 hour
        "media_folder": 86400,       # 24 hours
        "custom_field_set": 86400,   # 24 hours
        "category_custom_field_set": 86400,  # 24 hours
        "variant_option": 3600,      # 1 hour
        "currency": 86400,           # 24 hours
        "sales_channel": 86400,      # 24 hours
        "tax": 86400,                # 24 hours
        "field_mappings": 3600,      # 1 hour
        "image_hash": 86400,         # 24 hours (reduced from 7 days for better sync reliability)
    }

    # In-memory cache for the current request (faster than Redis for repeated access)
    # NOTE: This is now an instance attribute (set in __init__) for thread-safety.
    # Previously it was a class attribute which caused race conditions in multi-threaded contexts.

    def __init__(self):
        """Initialize the cache manager."""
        self._redis = frappe.cache()
        # Instance-level request cache for thread-safety
        self._request_cache: Dict[str, Any] = {}

    def _make_key(self, cache_type: str, key: str = "") -> str:
        """
        Create a Redis key for the given cache type and key.

        Args:
            cache_type: Type of cache (e.g., 'currency', 'category')
            key: Specific key within the cache type

        Returns:
            Full Redis key string
        """
        if key:
            return f"{self.REDIS_PREFIX}{cache_type}:{key}"
        return f"{self.REDIS_PREFIX}{cache_type}"

    def get(self, cache_type: str, key: str = "") -> Optional[Any]:
        """
        Get a value from cache.

        Checks in-memory request cache first, then Redis.

        Args:
            cache_type: Type of cache
            key: Specific key (optional for single-value caches)

        Returns:
            Cached value or None if not found
        """
        # Check request-level cache first (fastest)
        request_key = f"{cache_type}:{key}"
        if request_key in self._request_cache:
            return self._request_cache[request_key]

        # Check Redis cache
        redis_key = self._make_key(cache_type, key)
        value = self._redis.get_value(redis_key)

        # Store in request cache for subsequent access
        if value is not None:
            self._request_cache[request_key] = value

        return value

    def set(self, cache_type: str, key: str, value: Any, ttl: Optional[int] = None) -> None:
        """
        Set a value in cache.

        Args:
            cache_type: Type of cache
            key: Specific key
            value: Value to cache
            ttl: Time-to-live in seconds (uses default if not specified)
        """
        if ttl is None:
            ttl = self.DEFAULT_TTL.get(cache_type, 3600)

        # Set in Redis
        redis_key = self._make_key(cache_type, key)
        self._redis.set_value(redis_key, value, expires_in_sec=ttl)

        # Set in request cache
        request_key = f"{cache_type}:{key}"
        self._request_cache[request_key] = value

    def invalidate(self, cache_type: str, key: str = "") -> None:
        """
        Invalidate a specific cache entry.

        Args:
            cache_type: Type of cache
            key: Specific key (optional)
        """
        redis_key = self._make_key(cache_type, key)
        self._redis.delete_value(redis_key)

        # Remove from request cache
        request_key = f"{cache_type}:{key}"
        self._request_cache.pop(request_key, None)

    def invalidate_all(self, cache_type: str) -> None:
        """
        Invalidate all entries of a specific cache type.

        Note: This uses Redis key pattern matching which may be slow
        for large numbers of keys. Use sparingly.

        Args:
            cache_type: Type of cache to clear
        """
        pattern = self._make_key(cache_type, "*")
        keys = self._redis.get_keys(pattern)
        for key in keys:
            self._redis.delete_value(key)

        # Clear request cache entries of this type
        prefix = f"{cache_type}:"
        keys_to_remove = [k for k in self._request_cache if k.startswith(prefix)]
        for key in keys_to_remove:
            del self._request_cache[key]

    def clear_all(self) -> Dict[str, str]:
        """
        Clear all Shopware caches.

        Use this when Shopware configuration changes or for troubleshooting.

        Returns:
            Status message dict
        """
        # Clear all cache types
        for cache_type in self.DEFAULT_TTL.keys():
            self.invalidate_all(cache_type)

        # Clear request cache
        self._request_cache.clear()

        return {"status": "success", "message": "All Shopware caches cleared"}

    def get_or_fetch(
        self,
        cache_type: str,
        key: str,
        fetch_func: callable,
        ttl: Optional[int] = None
    ) -> Optional[Any]:
        """
        Get a value from cache or fetch it using the provided function.

        This is the preferred method for cache-aside pattern.

        Args:
            cache_type: Type of cache
            key: Specific key
            fetch_func: Function to call if cache miss (should return the value)
            ttl: Time-to-live in seconds (uses default if not specified)

        Returns:
            Cached or fetched value

        Example:
            currency_id = cache.get_or_fetch(
                "currency",
                "EUR",
                lambda: fetch_currency_from_api(client, "EUR")
            )
        """
        # Try cache first
        value = self.get(cache_type, key)
        if value is not None:
            return value

        # Fetch and cache
        value = fetch_func()
        if value is not None:
            self.set(cache_type, key, value, ttl)

        return value

    # Convenience methods for specific cache types

    def get_currency_id(self, currency_code: str = "EUR") -> Optional[str]:
        """Get cached currency ID."""
        return self.get("currency", currency_code)

    def set_currency_id(self, currency_code: str, currency_id: str) -> None:
        """Cache a currency ID."""
        self.set("currency", currency_code, currency_id)

    def get_sales_channel_id(self) -> Optional[str]:
        """Get cached default sales channel ID."""
        return self.get("sales_channel", "default")

    def set_sales_channel_id(self, sc_id: str) -> None:
        """Cache the default sales channel ID."""
        self.set("sales_channel", "default", sc_id)

    def get_category_id(self, item_group: str) -> Optional[str]:
        """Get cached Shopware category ID for an ERPNext Item Group."""
        return self.get("category", item_group)

    def set_category_id(self, item_group: str, category_id: str) -> None:
        """Cache a category ID mapping."""
        self.set("category", item_group, category_id)

    def get_property_group_id(self, group_name: str) -> Optional[str]:
        """Get cached property group ID."""
        return self.get("property_group", group_name)

    def set_property_group_id(self, group_name: str, group_id: str) -> None:
        """Cache a property group ID."""
        self.set("property_group", group_name, group_id)

    def get_property_option_id(self, group_name: str, option_name: str) -> Optional[str]:
        """Get cached property option ID."""
        key = f"{group_name}:{option_name}"
        return self.get("property_option", key)

    def set_property_option_id(self, group_name: str, option_name: str, option_id: str) -> None:
        """Cache a property option ID."""
        key = f"{group_name}:{option_name}"
        self.set("property_option", key, option_id)

    def get_image_hash(self, item_code: str, position: int) -> Optional[str]:
        """Get cached image hash for delta-sync."""
        key = f"{item_code}:{position}"
        return self.get("image_hash", key)

    def set_image_hash(self, item_code: str, position: int, file_hash: str) -> None:
        """Cache an image hash."""
        key = f"{item_code}:{position}"
        self.set("image_hash", key, file_hash)

    def clear_image_hashes(self, item_code: str, max_positions: int = 10) -> None:
        """Clear all image hashes for an item, including the count cache."""
        # Clear count cache (stored at position -1)
        self.invalidate("image_hash", f"{item_code}:-1")
        # Clear position hashes
        for position in range(max_positions):
            self.invalidate("image_hash", f"{item_code}:{position}")

    def get_field_mappings(self) -> Optional[Dict]:
        """Get cached field mappings configuration."""
        return self.get("field_mappings", "config")

    def set_field_mappings(self, mappings: Dict) -> None:
        """Cache field mappings configuration."""
        self.set("field_mappings", "config", mappings)

    def invalidate_field_mappings(self) -> None:
        """Invalidate field mappings cache (call when settings change)."""
        self.invalidate("field_mappings", "config")


# Thread-local storage for cache instances
# This ensures each thread gets its own cache instance with its own frappe.cache() context
import threading
_thread_local = threading.local()


def get_cache() -> ShopwareCacheManager:
    """
    Get a thread-local cache manager instance.

    THREAD-SAFETY: Each thread gets its own ShopwareCacheManager instance.
    This is critical for parallel sync operations where each thread has its own
    frappe.local context (after frappe.init()).

    The underlying Redis cache is shared (Redis is thread-safe), but the
    _request_cache (in-memory dict) is per-instance to avoid race conditions.

    Returns:
        ShopwareCacheManager: Thread-local cache instance
    """
    if not hasattr(_thread_local, 'cache_instance') or _thread_local.cache_instance is None:
        _thread_local.cache_instance = ShopwareCacheManager()
    return _thread_local.cache_instance


def reset_thread_cache():
    """
    Reset the cache instance for the current thread.
    
    Call this after frappe.destroy() in thread cleanup to ensure
    a fresh cache instance is created on the next get_cache() call.
    """
    if hasattr(_thread_local, 'cache_instance'):
        _thread_local.cache_instance = None


@frappe.whitelist()
def clear_shopware_cache() -> Dict[str, str]:
    """
    Clear all Shopware-related caches.

    Whitelisted for frontend/API access.

    Returns:
        Status message dict
    """
    require_shopware_admin()
    return get_cache().clear_all()
