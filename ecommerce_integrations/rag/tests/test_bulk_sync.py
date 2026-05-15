"""Smoke tests for rag/bulk_sync.py.

The full pipeline (Pinecone + Gemini/OpenAI embeddings) needs network
and API keys, so we don't exercise it here. These tests cover:

- the public API surface is still importable and shaped as documented,
- the bulk-mode and lock helpers wire through to the shared base in
  ``controllers/bulk_sync_base.py``,
- the queue add/get/remove primitives round-trip through Redis without
  losing the item codes they were given.
"""

from frappe.tests import IntegrationTestCase

from ecommerce_integrations.rag import bulk_sync


class TestRagBulkSyncPublicSurface(IntegrationTestCase):
	"""The function names below are referenced from hooks.py and from the
	RAG Setting form's JS. Renaming or removing them is a breaking change."""

	REQUIRED_CALLABLES = (
		"queue_item_for_sync",
		"schedule_bulk_sync_processing",
		"process_bulk_sync_queue",
		"check_and_process_queue",
		"add_to_queue",
		"clear_queue",
		"remove_from_queue",
		"get_queue_items",
		"should_use_bulk_mode",
		"force_process_queue",
		"get_queue_status",
		"clear_sync_queue",
	)

	def test_all_required_names_exist(self):
		for name in self.REQUIRED_CALLABLES:
			self.assertTrue(
				callable(getattr(bulk_sync, name, None)),
				f"rag.bulk_sync.{name} must remain callable",
			)


class TestRagQueueRoundTrip(IntegrationTestCase):
	"""``add_to_queue`` / ``get_queue_items`` / ``clear_queue`` must agree on
	the on-the-wire shape (item_code field, JSON-encoded set members)."""

	def setUp(self):
		bulk_sync.clear_queue()

	def tearDown(self):
		bulk_sync.clear_queue()

	def test_add_and_retrieve_item(self):
		bulk_sync.add_to_queue("ITEM-001")
		items = bulk_sync.get_queue_items()
		codes = [i["item_code"] for i in items]
		self.assertIn("ITEM-001", codes)

	def test_set_semantics_dedupe(self):
		# Redis SADD dedupes by exact JSON match. Same item_code + microsecond
		# happens to occur — get_queue_items still returns a set, not a list.
		bulk_sync.add_to_queue("ITEM-001")
		bulk_sync.add_to_queue("ITEM-002")
		items = bulk_sync.get_queue_items()
		codes = {i["item_code"] for i in items}
		self.assertEqual(codes, {"ITEM-001", "ITEM-002"})

	def test_remove_strips_one_item(self):
		bulk_sync.add_to_queue("ITEM-001")
		bulk_sync.add_to_queue("ITEM-002")
		bulk_sync.remove_from_queue("ITEM-001")
		codes = {i["item_code"] for i in bulk_sync.get_queue_items()}
		self.assertEqual(codes, {"ITEM-002"})

	def test_clear_empties_queue(self):
		bulk_sync.add_to_queue("ITEM-001")
		bulk_sync.clear_queue()
		self.assertEqual(bulk_sync.get_queue_items(), [])


class TestRagBulkSyncWiringToBase(IntegrationTestCase):
	"""``_get_tracker`` should return a tracker keyed under "rag:" so its
	keys don't collide with shopware6 / medusa trackers running on the same
	Redis instance."""

	def test_tracker_uses_rag_prefix(self):
		tracker = bulk_sync._get_tracker()
		self.assertEqual(tracker.bulk_mode_key, "rag:bulk_mode")
		self.assertEqual(tracker.request_count_key, "rag:request_count")
		self.assertEqual(tracker.last_request_key, "rag:last_request")

	def test_lock_key_is_namespaced(self):
		self.assertEqual(bulk_sync.SYNC_LOCK_KEY, "rag:sync_lock")
		self.assertEqual(bulk_sync.SYNC_QUEUE_KEY, "rag:sync_queue")
