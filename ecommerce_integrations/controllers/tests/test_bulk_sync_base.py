"""Integration tests for the cross-channel bulk-sync primitives.

Lock and BulkModeTracker exercise the live Redis-backed Frappe cache,
so these run as IntegrationTestCases — there's no value in mocking the
cache out: that's the one thing the atomicity actually depends on.
Each test sets up its own redis-key prefix and cleans up after itself
so the suite can run on a shared site without pollution.
"""

import frappe
from frappe.tests import IntegrationTestCase

from ecommerce_integrations.controllers.bulk_sync_base import (
	BulkModeTracker,
	acquire_lock,
	release_lock,
	unique_extend,
)


class TestAcquireLock(IntegrationTestCase):
	def setUp(self):
		self.key = "_test_bulk_sync_lock"
		frappe.cache.delete(self.key)

	def tearDown(self):
		frappe.cache.delete(self.key)

	def test_first_acquire_succeeds(self):
		self.assertTrue(acquire_lock(self.key, ttl_seconds=10))

	def test_second_acquire_while_held_fails(self):
		self.assertTrue(acquire_lock(self.key, ttl_seconds=10))
		self.assertFalse(acquire_lock(self.key, ttl_seconds=10))

	def test_acquire_after_release_succeeds(self):
		self.assertTrue(acquire_lock(self.key, ttl_seconds=10))
		release_lock(self.key)
		self.assertTrue(acquire_lock(self.key, ttl_seconds=10))

	def test_release_of_unheld_lock_is_safe(self):
		# Best-effort delete; the function must not raise when the key is gone.
		release_lock(self.key)
		release_lock(self.key)


class TestBulkModeTracker(IntegrationTestCase):
	def setUp(self):
		self.prefix = "_test_bmt"
		self.tracker = BulkModeTracker(
			key_prefix=self.prefix, threshold=3, window=2, cooldown=10
		)
		self._cleanup()

	def tearDown(self):
		self._cleanup()

	def _cleanup(self):
		for suffix in ("bulk_mode", "request_count", "last_request"):
			frappe.cache.delete_value(f"{self.prefix}:{suffix}")

	def test_starts_inactive(self):
		self.assertFalse(self.tracker.is_active())

	def test_activates_on_threshold_tick(self):
		# threshold=3 → first two ticks should not flip, third tick should.
		self.assertFalse(self.tracker.should_activate())
		self.assertFalse(self.tracker.should_activate())
		self.assertTrue(self.tracker.should_activate())
		self.assertTrue(self.tracker.is_active())

	def test_deactivate_clears_key(self):
		self.tracker.activate()
		self.assertTrue(self.tracker.is_active())
		self.tracker.deactivate()
		self.assertFalse(self.tracker.is_active())

	def test_should_activate_returns_true_when_already_active(self):
		# Once bulk mode is on, every subsequent tick reports True without
		# touching the request counter — the API contract for queue
		# producers that need to know whether to defer.
		self.tracker.activate()
		self.assertTrue(self.tracker.should_activate())
		self.assertTrue(self.tracker.should_activate())

	def test_independent_prefixes_track_independently(self):
		other = BulkModeTracker(
			key_prefix=self.prefix + "_other",
			threshold=3, window=2, cooldown=10,
		)
		try:
			self.tracker.activate()
			self.assertTrue(self.tracker.is_active())
			self.assertFalse(other.is_active())
		finally:
			for suffix in ("bulk_mode", "request_count", "last_request"):
				frappe.cache.delete_value(f"{self.prefix}_other:{suffix}")


class TestUniqueExtend(IntegrationTestCase):
	def test_appends_new(self):
		out = unique_extend(["a"], ["b", "c"])
		self.assertEqual(out, ["a", "b", "c"])

	def test_skips_duplicates(self):
		out = unique_extend(["a"], ["a", "b"])
		self.assertEqual(out, ["a", "b"])

	def test_mutates_in_place(self):
		target = ["a"]
		unique_extend(target, ["b"])
		self.assertEqual(target, ["a", "b"])
