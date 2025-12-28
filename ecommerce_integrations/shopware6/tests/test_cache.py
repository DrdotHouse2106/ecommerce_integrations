"""
Tests for Shopware 6 Cache Manager
"""

import unittest
from unittest.mock import patch, MagicMock
import time

from ecommerce_integrations.shopware6.tests.utils import ShopwareTestCase


class TestShopwareCacheManager(ShopwareTestCase):
    """Test cases for ShopwareCacheManager."""

    @patch("ecommerce_integrations.shopware6.base.cache_manager.frappe")
    def test_cache_initialization(self, mock_frappe):
        """Test cache manager initializes correctly."""
        from ecommerce_integrations.shopware6.base.cache_manager import ShopwareCacheManager

        mock_frappe.cache.return_value = MagicMock()
        cache = ShopwareCacheManager()
        self.assertIsNotNone(cache)

    @patch("ecommerce_integrations.shopware6.base.cache_manager.frappe")
    def test_cache_set_get(self, mock_frappe):
        """Test basic cache set and get operations."""
        from ecommerce_integrations.shopware6.base.cache_manager import ShopwareCacheManager

        mock_cache = MagicMock()
        mock_cache.get.return_value = None
        mock_frappe.cache.return_value = mock_cache

        cache = ShopwareCacheManager()

        # Set a value
        cache.set("test_key", "test_value", ttl=300)
        mock_cache.set_value.assert_called()

        # Get the value
        mock_cache.get_value.return_value = {"value": "test_value", "expires_at": time.time() + 300}
        result = cache.get("test_key")
        self.assertEqual(result, "test_value")

    @patch("ecommerce_integrations.shopware6.base.cache_manager.frappe")
    def test_cache_expiration(self, mock_frappe):
        """Test that expired cache entries return None."""
        from ecommerce_integrations.shopware6.base.cache_manager import ShopwareCacheManager

        mock_cache = MagicMock()
        # Return expired entry
        mock_cache.get_value.return_value = {
            "value": "old_value",
            "expires_at": time.time() - 100  # Expired 100 seconds ago
        }
        mock_frappe.cache.return_value = mock_cache

        cache = ShopwareCacheManager()
        result = cache.get("expired_key")

        # Should return None for expired entries
        self.assertIsNone(result)

    @patch("ecommerce_integrations.shopware6.base.cache_manager.frappe")
    def test_cache_invalidate(self, mock_frappe):
        """Test cache invalidation."""
        from ecommerce_integrations.shopware6.base.cache_manager import ShopwareCacheManager

        mock_cache = MagicMock()
        mock_frappe.cache.return_value = mock_cache

        cache = ShopwareCacheManager()
        cache.invalidate("some_key")

        mock_cache.delete_value.assert_called()

    @patch("ecommerce_integrations.shopware6.base.cache_manager.frappe")
    def test_cache_invalidate_pattern(self, mock_frappe):
        """Test cache invalidation by pattern."""
        from ecommerce_integrations.shopware6.base.cache_manager import ShopwareCacheManager

        mock_cache = MagicMock()
        mock_cache.get_keys.return_value = [
            "shopware:category:1",
            "shopware:category:2",
            "shopware:category:3"
        ]
        mock_frappe.cache.return_value = mock_cache

        cache = ShopwareCacheManager()
        cache.invalidate_pattern("category")

        # Should delete all matching keys
        self.assertEqual(mock_cache.delete_value.call_count, 3)

    @patch("ecommerce_integrations.shopware6.base.cache_manager.frappe")
    def test_get_or_fetch_cache_hit(self, mock_frappe):
        """Test get_or_fetch returns cached value on cache hit."""
        from ecommerce_integrations.shopware6.base.cache_manager import ShopwareCacheManager

        mock_cache = MagicMock()
        mock_cache.get_value.return_value = {
            "value": "cached_value",
            "expires_at": time.time() + 300
        }
        mock_frappe.cache.return_value = mock_cache

        cache = ShopwareCacheManager()
        fetch_fn = MagicMock(return_value="fresh_value")

        result = cache.get_or_fetch("test_key", fetch_fn)

        # Should return cached value
        self.assertEqual(result, "cached_value")
        # Fetch function should not be called
        fetch_fn.assert_not_called()

    @patch("ecommerce_integrations.shopware6.base.cache_manager.frappe")
    def test_get_or_fetch_cache_miss(self, mock_frappe):
        """Test get_or_fetch calls fetch function on cache miss."""
        from ecommerce_integrations.shopware6.base.cache_manager import ShopwareCacheManager

        mock_cache = MagicMock()
        mock_cache.get_value.return_value = None  # Cache miss
        mock_frappe.cache.return_value = mock_cache

        cache = ShopwareCacheManager()
        fetch_fn = MagicMock(return_value="fresh_value")

        result = cache.get_or_fetch("test_key", fetch_fn, ttl=300)

        # Should return fresh value
        self.assertEqual(result, "fresh_value")
        # Fetch function should be called
        fetch_fn.assert_called_once()
        # Value should be cached
        mock_cache.set_value.assert_called()


class TestCacheConvenienceMethods(ShopwareTestCase):
    """Test cases for cache convenience methods."""

    @patch("ecommerce_integrations.shopware6.base.cache_manager.frappe")
    def test_get_currency_id(self, mock_frappe):
        """Test getting cached currency ID."""
        from ecommerce_integrations.shopware6.base.cache_manager import ShopwareCacheManager

        mock_cache = MagicMock()
        mock_cache.get_value.return_value = {
            "value": "currency-eur-id",
            "expires_at": time.time() + 300
        }
        mock_frappe.cache.return_value = mock_cache

        cache = ShopwareCacheManager()
        result = cache.get_currency_id("EUR")

        self.assertEqual(result, "currency-eur-id")

    @patch("ecommerce_integrations.shopware6.base.cache_manager.frappe")
    def test_get_sales_channel_id(self, mock_frappe):
        """Test getting cached sales channel ID."""
        from ecommerce_integrations.shopware6.base.cache_manager import ShopwareCacheManager

        mock_cache = MagicMock()
        mock_cache.get_value.return_value = {
            "value": "sales-channel-storefront",
            "expires_at": time.time() + 300
        }
        mock_frappe.cache.return_value = mock_cache

        cache = ShopwareCacheManager()
        result = cache.get_sales_channel_id()

        self.assertEqual(result, "sales-channel-storefront")

    @patch("ecommerce_integrations.shopware6.base.cache_manager.frappe")
    def test_get_category_id(self, mock_frappe):
        """Test getting cached category ID."""
        from ecommerce_integrations.shopware6.base.cache_manager import ShopwareCacheManager

        mock_cache = MagicMock()
        mock_cache.get_value.return_value = {
            "value": "category-clothing-id",
            "expires_at": time.time() + 300
        }
        mock_frappe.cache.return_value = mock_cache

        cache = ShopwareCacheManager()
        result = cache.get_category_id("Clothing")

        self.assertEqual(result, "category-clothing-id")

    @patch("ecommerce_integrations.shopware6.base.cache_manager.frappe")
    def test_get_image_hash(self, mock_frappe):
        """Test getting cached image hash for delta sync."""
        from ecommerce_integrations.shopware6.base.cache_manager import ShopwareCacheManager

        mock_cache = MagicMock()
        mock_cache.get_value.return_value = {
            "value": "abc123def456",
            "expires_at": time.time() + 300
        }
        mock_frappe.cache.return_value = mock_cache

        cache = ShopwareCacheManager()
        result = cache.get_image_hash("product-123", "image-1.jpg")

        self.assertEqual(result, "abc123def456")

    @patch("ecommerce_integrations.shopware6.base.cache_manager.frappe")
    def test_set_image_hash(self, mock_frappe):
        """Test setting image hash for delta sync."""
        from ecommerce_integrations.shopware6.base.cache_manager import ShopwareCacheManager

        mock_cache = MagicMock()
        mock_frappe.cache.return_value = mock_cache

        cache = ShopwareCacheManager()
        cache.set_image_hash("product-123", "image-1.jpg", "new-hash-789")

        mock_cache.set_value.assert_called()


if __name__ == "__main__":
    unittest.main()
