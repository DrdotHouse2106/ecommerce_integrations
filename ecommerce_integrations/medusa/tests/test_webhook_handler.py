"""Smoke tests for medusa/webhook_handler.py.

Covers the security and routing contract:

- the dispatch table maps known events to their handler dotted paths,
- the public surface (``handle_medusa_event``) stays importable and
  whitelisted,
- the routing helper enqueues the correct handler for each event,
- payment-sync side-channels fire only on update/complete events.

Full end-to-end signature/dedup/auth behaviour requires a live HTTP
request context (``frappe.request``) and a real Redis cache. Those paths
are exercised by the integration suite; here we keep the unit-level
guard so renaming a handler key is caught at test-time.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from ecommerce_integrations.medusa import webhook_handler


class TestMedusaWebhookPublicSurface(IntegrationTestCase):
    """``handle_medusa_event`` is referenced from the Medusa Setting form and
    the Medusa subscriber plugin by dotted path. Renaming or dropping it is a
    breaking change for installed sites and existing subscribers."""

    def test_handler_is_callable(self):
        self.assertTrue(callable(getattr(webhook_handler, "handle_medusa_event", None)))

    def test_handler_is_whitelisted_allow_guest(self):
        # Webhooks come from Medusa with no session cookie; the function must
        # be whitelisted for guest access or the subscriber gets a 403.
        handler = webhook_handler.handle_medusa_event
        # ``frappe.whitelist`` sets these attributes on the wrapper.
        self.assertTrue(
            getattr(handler, "is_whitelisted", False)
            or getattr(handler, "_is_whitelisted", False)
            or getattr(handler, "whitelisted", False),
            "handle_medusa_event must remain @frappe.whitelist(allow_guest=True)",
        )


class TestRouteEvent(unittest.TestCase):
    """``_route_event`` is the dispatch table that turns Medusa event names
    into background-queue enqueues. The map is load-bearing — adding a new
    event must land here and in the dispatch map together."""

    @patch("ecommerce_integrations.medusa.webhook_handler.frappe")
    def test_order_placed_enqueues_order_sync(self, mock_frappe):
        webhook_handler._route_event("order.placed", {"id": "order_01ABC"})
        # Exactly one enqueue (no payment side-channel on placed).
        self.assertEqual(mock_frappe.enqueue.call_count, 1)
        call = mock_frappe.enqueue.call_args
        self.assertEqual(
            call.args[0],
            "ecommerce_integrations.medusa.order.order_sync.sync_order",
        )
        self.assertEqual(call.kwargs["entity_id"], "order_01ABC")
        self.assertEqual(call.kwargs["event_type"], "order.placed")

    @patch("ecommerce_integrations.medusa.webhook_handler.frappe")
    def test_order_updated_fires_payment_sync_too(self, mock_frappe):
        webhook_handler._route_event("order.updated", {"id": "order_01ABC"})
        # Two enqueues: order sync + payment sync.
        self.assertEqual(mock_frappe.enqueue.call_count, 2)
        targets = [c.args[0] for c in mock_frappe.enqueue.call_args_list]
        self.assertIn(
            "ecommerce_integrations.medusa.order.order_sync.sync_order",
            targets,
        )
        self.assertIn(
            "ecommerce_integrations.medusa.payment_sync.sync_payment_for_order",
            targets,
        )

    @patch("ecommerce_integrations.medusa.webhook_handler.frappe")
    def test_customer_created_enqueues_customer_sync(self, mock_frappe):
        webhook_handler._route_event(
            "customer.created", {"id": "cus_01ABC"},
        )
        self.assertEqual(mock_frappe.enqueue.call_count, 1)
        self.assertEqual(
            mock_frappe.enqueue.call_args.args[0],
            "ecommerce_integrations.medusa.customer.sync_customer_by_id",
        )

    @patch("ecommerce_integrations.medusa.webhook_handler.frappe")
    def test_unknown_event_does_not_enqueue(self, mock_frappe):
        webhook_handler._route_event("nonsense.event", {"id": "x"})
        mock_frappe.enqueue.assert_not_called()

    @patch("ecommerce_integrations.medusa.webhook_handler.frappe")
    def test_customer_deleted_routes_to_delete_handler(self, mock_frappe):
        webhook_handler._route_event("customer.deleted", {"id": "cus_01ABC"})
        self.assertEqual(mock_frappe.enqueue.call_count, 1)
        self.assertEqual(
            mock_frappe.enqueue.call_args.args[0],
            "ecommerce_integrations.medusa.customer.handle_customer_deleted",
        )


# NOTE: signature verification / dedup / disabled-integration paths in
# handle_medusa_event itself need a real frappe.request and a fresh Redis;
# they belong in the live integration suite. The unit-level guards above
# protect the wiring that's load-bearing for installed subscribers.

if __name__ == "__main__":
    unittest.main()
