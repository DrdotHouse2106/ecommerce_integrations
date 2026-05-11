"""Smoke tests for medusa/bulk_sync.py.

Verifies that the public API surface stays intact, that the
``_get_tracker``-style wiring talks to the shared
``controllers.bulk_sync_base`` with the right key prefix, and that the
JSON-list queue round-trips through Redis without losing items.
"""

import frappe
from frappe.tests import IntegrationTestCase

from ecommerce_integrations.medusa import bulk_sync


class TestMedusaBulkSyncPublicSurface(IntegrationTestCase):
	"""Hook entries in ``hooks.py`` and the Medusa Setting form's JS reference
	these by dotted path. Renaming a function or dropping it is a breaking
	change for installed sites."""

	REQUIRED_CALLABLES = (
		"queue_item_for_sync",
		"queue_price_for_sync",
		"check_and_process_queue",
		"process_queue",
	)

	def test_all_required_names_exist(self):
		for name in self.REQUIRED_CALLABLES:
			self.assertTrue(
				callable(getattr(bulk_sync, name, None)),
				f"medusa.bulk_sync.{name} must remain callable",
			)


class TestMedusaQueueRoundTrip(IntegrationTestCase):
	def setUp(self):
		frappe.cache.delete_value(bulk_sync.QUEUE_KEY)
		frappe.cache.delete_value(bulk_sync.PRICE_QUEUE_KEY)

	def tearDown(self):
		frappe.cache.delete_value(bulk_sync.QUEUE_KEY)
		frappe.cache.delete_value(bulk_sync.PRICE_QUEUE_KEY)

	def test_add_and_get_product_queue(self):
		bulk_sync._add_to_queue("ITEM-001", bulk_sync.QUEUE_KEY)
		self.assertEqual(bulk_sync._get_queue(bulk_sync.QUEUE_KEY), ["ITEM-001"])

	def test_add_dedupes(self):
		bulk_sync._add_to_queue("ITEM-001", bulk_sync.QUEUE_KEY)
		bulk_sync._add_to_queue("ITEM-001", bulk_sync.QUEUE_KEY)
		bulk_sync._add_to_queue("ITEM-002", bulk_sync.QUEUE_KEY)
		self.assertEqual(
			bulk_sync._get_queue(bulk_sync.QUEUE_KEY),
			["ITEM-001", "ITEM-002"],
		)

	def test_remove_strips_items(self):
		bulk_sync._add_to_queue("ITEM-001", bulk_sync.QUEUE_KEY)
		bulk_sync._add_to_queue("ITEM-002", bulk_sync.QUEUE_KEY)
		bulk_sync._remove_from_queue(["ITEM-001"], bulk_sync.QUEUE_KEY)
		self.assertEqual(bulk_sync._get_queue(bulk_sync.QUEUE_KEY), ["ITEM-002"])

	def test_product_and_price_queues_are_independent(self):
		bulk_sync._add_to_queue("ITEM-001", bulk_sync.QUEUE_KEY)
		bulk_sync._add_to_queue("ITEM-002", bulk_sync.PRICE_QUEUE_KEY)
		self.assertEqual(bulk_sync._get_queue(bulk_sync.QUEUE_KEY), ["ITEM-001"])
		self.assertEqual(bulk_sync._get_queue(bulk_sync.PRICE_QUEUE_KEY), ["ITEM-002"])


class TestMedusaBulkSyncWiringToBase(IntegrationTestCase):
	"""``_TRACKER`` and the lock helpers should namespace under "medusa:" so
	they don't collide with shopware6 / rag trackers on the same Redis."""

	def test_tracker_uses_medusa_prefix(self):
		self.assertEqual(bulk_sync._TRACKER.bulk_mode_key, "medusa:bulk_mode")
		self.assertEqual(bulk_sync._TRACKER.request_count_key, "medusa:request_count")
		self.assertEqual(bulk_sync._TRACKER.last_request_key, "medusa:last_request")

	def test_lock_key_namespaced(self):
		self.assertEqual(bulk_sync.LOCK_KEY, "medusa:sync_lock")

	def test_acquire_release_is_atomic(self):
		frappe.cache.delete(bulk_sync.LOCK_KEY)
		try:
			self.assertTrue(bulk_sync._acquire_lock(timeout=10))
			self.assertFalse(bulk_sync._acquire_lock(timeout=10))
			bulk_sync._release_lock()
			self.assertTrue(bulk_sync._acquire_lock(timeout=10))
		finally:
			bulk_sync._release_lock()
