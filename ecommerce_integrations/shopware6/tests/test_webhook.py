"""Tests for the Shopware 6 webhook dispatch table and handler.

Exercises ``EVENT_HANDLERS``, ``WebhookHandler.handle`` and
``WebhookEventQueue.__init__``. Full dedup/lock behaviour is integration-
covered against a live Redis.
"""

import unittest
from unittest.mock import patch

from ecommerce_integrations.shopware6.tests.utils import (
	ShopwareTestCase,
	TEST_ORDER_UUID,
)


class TestEventHandlerDispatchTable(unittest.TestCase):
	"""``EVENT_HANDLERS`` is the source of truth for which Shopware
	webhook events the integration knows how to route. Adding a new
	event type should land in this dict; tests guard against accidental
	deletion of the well-known entries."""

	REQUIRED_EVENTS = (
		"order.placed",
		"order.written",
		"order.deleted",
		"checkout.order.placed",
		# Shopware emits state changes per-entity, not as a single
		# ``order.status.changed`` — keep all three.
		"order.state_machine_state.changed",
		"order_transaction.state_machine_state.changed",
		"order_delivery.state_machine_state.changed",
		"product.written",
		"product.deleted",
		"customer.written",
		"customer.deleted",
		"product_stock.written",
	)

	def test_required_events_have_handlers(self):
		from ecommerce_integrations.shopware6.webhook.handler import EVENT_HANDLERS

		for event in self.REQUIRED_EVENTS:
			self.assertIn(
				event, EVENT_HANDLERS,
				f"Missing handler entry for {event}",
			)

	def test_handler_names_match_method_naming(self):
		# Every value should be a plain identifier — they're looked up via
		# ``getattr(self, handler_name)`` on the WebhookHandler instance.
		import re
		from ecommerce_integrations.shopware6.webhook.handler import EVENT_HANDLERS

		ident = re.compile(r"^[a-z_][a-z0-9_]*$")
		for event, handler in EVENT_HANDLERS.items():
			self.assertTrue(
				ident.match(handler),
				f"Handler {handler!r} for {event} is not a valid attribute name",
			)


class TestWebhookHandlerDispatch(ShopwareTestCase):
	"""``WebhookHandler.handle`` should: gate on settings.is_enabled, fall
	through gracefully on unknown event types, and forward to the matching
	method on the handler instance for known events."""

	@patch("ecommerce_integrations.shopware6.webhook.handler.ShopwareDataValidator")
	@patch("ecommerce_integrations.shopware6.webhook.handler.frappe")
	def test_returns_false_when_integration_disabled(self, mock_frappe, mock_validator):
		from ecommerce_integrations.shopware6.webhook.handler import WebhookHandler

		mock_setting = self.make_enabled_setting_mock()
		mock_setting.is_enabled.return_value = False
		mock_frappe.get_cached_doc.return_value = mock_setting

		handler = WebhookHandler()
		result = handler.handle("order.placed", TEST_ORDER_UUID, {})
		self.assertFalse(result)
		mock_validator.validate_webhook_payload.assert_not_called()

	@patch("ecommerce_integrations.shopware6.webhook.handler.ShopwareDataValidator")
	@patch("ecommerce_integrations.shopware6.webhook.handler.frappe")
	def test_returns_true_for_unknown_event_type(self, mock_frappe, mock_validator):
		from ecommerce_integrations.shopware6.webhook.handler import WebhookHandler

		mock_frappe.get_cached_doc.return_value = self.make_enabled_setting_mock()
		mock_validator.validate_webhook_payload.return_value = (True, [])

		# Unknown event types must succeed — returning False would have
		# Shopware retry forever for events we haven't built handlers for.
		handler = WebhookHandler()
		result = handler.handle("something.unknown", TEST_ORDER_UUID, {"primaryKey": "x"})
		self.assertTrue(result)

	@patch("ecommerce_integrations.shopware6.webhook.handler.ShopwareDataValidator")
	@patch("ecommerce_integrations.shopware6.webhook.handler.frappe")
	def test_returns_false_for_invalid_payload(self, mock_frappe, mock_validator):
		from ecommerce_integrations.shopware6.webhook.handler import WebhookHandler

		mock_frappe.get_cached_doc.return_value = self.make_enabled_setting_mock()
		mock_validator.validate_webhook_payload.return_value = (False, ["missing field"])

		handler = WebhookHandler()
		result = handler.handle("order.placed", TEST_ORDER_UUID, {})
		self.assertFalse(result)


class TestWebhookEventQueue(ShopwareTestCase):
	"""WebhookEventQueue construction. The dedup/lock path itself needs
	Redis + real state-machine arrivals; covered by integration tests."""

	def test_event_queue_initialization(self):
		from ecommerce_integrations.shopware6.webhook.event_queue import (
			WebhookEventQueue,
		)
		queue = WebhookEventQueue()
		self.assertIsNotNone(queue.cache)


if __name__ == "__main__":
	unittest.main()
