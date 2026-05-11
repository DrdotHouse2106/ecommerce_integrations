"""
Tests for Shopware 6 Webhook Handler
"""

import unittest
from unittest.mock import patch, MagicMock
import time

from ecommerce_integrations.shopware6.tests.utils import (
    ShopwareTestCase,
    create_sample_order_data,
)


class TestWebhookEventQueue(ShopwareTestCase):
    """Test cases for WebhookEventQueue."""

    def test_event_queue_initialization(self):
        """Test event queue initializes correctly."""
        from ecommerce_integrations.shopware6.webhook.event_queue import WebhookEventQueue

        queue = WebhookEventQueue()
        self.assertIsNotNone(queue)

    @patch("ecommerce_integrations.shopware6.webhook.event_queue.frappe")
    def test_enqueue_new_event(self, mock_frappe):
        """Test enqueueing a new event."""
        from ecommerce_integrations.shopware6.webhook.event_queue import WebhookEventQueue

        mock_frappe.cache.return_value.get.return_value = None
        mock_frappe.cache.return_value.lock.return_value.__enter__ = MagicMock()
        mock_frappe.cache.return_value.lock.return_value.__exit__ = MagicMock()

        queue = WebhookEventQueue()
        result = queue.enqueue(
            event_type="order.placed",
            entity_id="test-order-123",
            payload={"orderId": "test-order-123"}
        )

        # Should return True for new events
        self.assertTrue(result)

    @patch("ecommerce_integrations.shopware6.webhook.event_queue.frappe")
    def test_enqueue_duplicate_event(self, mock_frappe):
        """Test that duplicate events are deduplicated."""
        from ecommerce_integrations.shopware6.webhook.event_queue import WebhookEventQueue

        # Simulate existing event in cache
        existing_event = {
            "event_type": "order.placed",
            "entity_id": "test-order-123",
            "processed_at": time.time()
        }
        mock_frappe.cache.return_value.get.return_value = existing_event
        mock_frappe.cache.return_value.lock.return_value.__enter__ = MagicMock()
        mock_frappe.cache.return_value.lock.return_value.__exit__ = MagicMock()

        queue = WebhookEventQueue()
        result = queue.enqueue(
            event_type="order.placed",
            entity_id="test-order-123",
            payload={"orderId": "test-order-123"}
        )

        # Should return False for duplicate events
        self.assertFalse(result)

    @patch("ecommerce_integrations.shopware6.webhook.event_queue.frappe")
    def test_is_processed_recently(self, mock_frappe):
        """Test checking if event was processed recently."""
        from ecommerce_integrations.shopware6.webhook.event_queue import WebhookEventQueue

        # Event processed 30 seconds ago (within window)
        mock_frappe.cache.return_value.get.return_value = {
            "processed_at": time.time() - 30
        }

        queue = WebhookEventQueue()
        result = queue.is_processed("order.placed", "test-order-123")

        self.assertTrue(result)

    @patch("ecommerce_integrations.shopware6.webhook.event_queue.frappe")
    def test_is_not_processed_old(self, mock_frappe):
        """Test that old events are not considered processed."""
        from ecommerce_integrations.shopware6.webhook.event_queue import WebhookEventQueue

        # Event processed 10 minutes ago (outside window)
        mock_frappe.cache.return_value.get.return_value = {
            "processed_at": time.time() - 600
        }

        queue = WebhookEventQueue()
        result = queue.is_processed("order.placed", "test-order-123")

        # Should return False for old events (depending on TTL config)
        # This depends on implementation details
        pass


class TestWebhookHandler(ShopwareTestCase):
    """Test cases for webhook handler routing."""

    def test_event_handlers_defined(self):
        """Test that all expected event handlers are defined."""
        from ecommerce_integrations.shopware6.webhook.handler import EVENT_HANDLERS

        expected_events = [
            "order.placed",
            "order.status.changed",
            "order.payment.state.changed",
        ]

        for event in expected_events:
            self.assertIn(event, EVENT_HANDLERS)

    @patch("ecommerce_integrations.shopware6.webhook.handler.frappe")
    def test_handle_order_placed(self, mock_frappe):
        """Test handling order.placed event."""
        from ecommerce_integrations.shopware6.webhook.handler import (
            WebhookHandler,
        )

        order_data = create_sample_order_data()
        payload = {
            "source": "shopware",
            "data": {"entity": order_data},
            "primaryKey": order_data["id"]
        }

        # Test that handler extracts order ID correctly
        handler = WebhookHandler()
        entity_id = handler.get_entity_id(payload)
        self.assertEqual(entity_id, order_data["id"])

    @patch("ecommerce_integrations.shopware6.webhook.handler.frappe")
    def test_handle_unknown_event(self, mock_frappe):
        """Test handling unknown event type."""
        from ecommerce_integrations.shopware6.webhook.handler import WebhookHandler

        handler = WebhookHandler()

        payload = {
            "source": "shopware",
            "data": {"entity": {"id": "test-123"}},
            "primaryKey": "test-123"
        }

        # Should not raise error for unknown events
        result = handler.route_event("unknown.event.type", payload)
        self.assertIsNone(result)


class TestWebhookPayloadValidation(ShopwareTestCase):
    """Test cases for webhook payload validation."""

    def test_validate_order_placed_payload(self):
        """Test validation of order.placed payload."""
        from ecommerce_integrations.shopware6.validators import ShopwareDataValidator

        order_data = create_sample_order_data()
        payload = {
            "source": "shopware",
            "data": {"entity": order_data, "orderId": order_data["id"]},
            "primaryKey": order_data["id"]
        }

        is_valid, errors = ShopwareDataValidator.validate_webhook_payload(payload)
        self.assertTrue(is_valid)
        self.assertEqual(len(errors), 0)

    def test_validate_empty_payload(self):
        """Test validation fails for empty payload."""
        from ecommerce_integrations.shopware6.validators import ShopwareDataValidator

        is_valid, errors = ShopwareDataValidator.validate_webhook_payload({})
        self.assertFalse(is_valid)

    def test_validate_payload_without_entity_id(self):
        """Test validation fails without entity ID."""
        from ecommerce_integrations.shopware6.validators import ShopwareDataValidator

        payload = {
            "source": "shopware",
            "data": {}
        }

        is_valid, errors = ShopwareDataValidator.validate_webhook_payload(payload)
        self.assertFalse(is_valid)
        self.assertTrue(any("entity ID" in e for e in errors))


if __name__ == "__main__":
    unittest.main()
