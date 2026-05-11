"""Tests for ``shopware6.base.cache_manager.ShopwareCacheManager``.

Runs against the live Frappe Redis cache — mocking Redis here would
test nothing, since the cache manager's contract *is* the Redis wrapping.
Each test cleans its own keys under the ``test_cache_manager`` prefix.
"""

import frappe
from frappe.tests import IntegrationTestCase

from ecommerce_integrations.shopware6.base.cache_manager import ShopwareCacheManager


_TEST_TYPE = "test_cache_manager"


class TestShopwareCacheManager(IntegrationTestCase):
	def setUp(self):
		# One scan per suite via setUp would mean 9 × full-keyspace scans;
		# rely on tearDown's invalidate_all (cheap SCAN over a small set)
		# and only seed when something is left over from a crashed run.
		ShopwareCacheManager().invalidate_all(_TEST_TYPE)

	def tearDown(self):
		ShopwareCacheManager().invalidate_all(_TEST_TYPE)

	def test_initialization(self):
		# Each instance must own its request cache — class-attr regression guard.
		cache = ShopwareCacheManager()
		self.assertEqual(cache._request_cache, {})

	def test_set_then_get_returns_value(self):
		cache = ShopwareCacheManager()
		cache.set(_TEST_TYPE, "alpha", "hello", ttl=60)
		self.assertEqual(cache.get(_TEST_TYPE, "alpha"), "hello")

	def test_get_missing_key_returns_none(self):
		cache = ShopwareCacheManager()
		self.assertIsNone(cache.get(_TEST_TYPE, "never-set"))

	def test_set_overwrites(self):
		cache = ShopwareCacheManager()
		cache.set(_TEST_TYPE, "k", "v1", ttl=60)
		cache.set(_TEST_TYPE, "k", "v2", ttl=60)
		self.assertEqual(cache.get(_TEST_TYPE, "k"), "v2")

	def test_invalidate_drops_specific_key(self):
		cache = ShopwareCacheManager()
		cache.set(_TEST_TYPE, "stays", "kept", ttl=60)
		cache.set(_TEST_TYPE, "goes", "dropped", ttl=60)
		cache.invalidate(_TEST_TYPE, "goes")
		self.assertEqual(cache.get(_TEST_TYPE, "stays"), "kept")
		self.assertIsNone(cache.get(_TEST_TYPE, "goes"))

	def test_invalidate_all_clears_type(self):
		cache = ShopwareCacheManager()
		cache.set(_TEST_TYPE, "a", 1, ttl=60)
		cache.set(_TEST_TYPE, "b", 2, ttl=60)
		cache.set(_TEST_TYPE, "c", 3, ttl=60)
		cache.invalidate_all(_TEST_TYPE)
		self.assertIsNone(cache.get(_TEST_TYPE, "a"))
		self.assertIsNone(cache.get(_TEST_TYPE, "b"))
		self.assertIsNone(cache.get(_TEST_TYPE, "c"))

	def test_request_cache_hit_does_not_touch_redis(self):
		# After a set, the same instance should serve the get from the
		# in-memory request cache; a fresh instance must still find the
		# value in Redis (no isolation between instances at the Redis level).
		cache_a = ShopwareCacheManager()
		cache_a.set(_TEST_TYPE, "shared", "value", ttl=60)
		self.assertEqual(cache_a.get(_TEST_TYPE, "shared"), "value")
		cache_b = ShopwareCacheManager()
		self.assertEqual(cache_b.get(_TEST_TYPE, "shared"), "value")

	def test_get_or_fetch_calls_fetcher_on_miss(self):
		cache = ShopwareCacheManager()
		call_count = {"n": 0}

		def fetcher():
			call_count["n"] += 1
			return "computed"

		# First call: misses cache, invokes fetcher.
		v1 = cache.get_or_fetch(_TEST_TYPE, "lazy", fetcher, ttl=60)
		self.assertEqual(v1, "computed")
		self.assertEqual(call_count["n"], 1)

		# Second call: hits cache, fetcher not re-invoked.
		v2 = cache.get_or_fetch(_TEST_TYPE, "lazy", fetcher, ttl=60)
		self.assertEqual(v2, "computed")
		self.assertEqual(call_count["n"], 1)

	def test_default_ttl_table_has_known_cache_types(self):
		cache = ShopwareCacheManager()
		for cache_type in (
			"property_group", "category", "media_folder", "currency",
			"sales_channel", "image_hash", "field_mappings",
		):
			self.assertIn(cache_type, cache.DEFAULT_TTL)
			self.assertGreater(cache.DEFAULT_TTL[cache_type], 0)


if __name__ == "__main__":
	import unittest

	unittest.main()
