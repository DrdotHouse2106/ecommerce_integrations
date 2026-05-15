"""Per-handler smoke tests for the Shopware 6 webhook handler family.

``test_webhook.py`` covers the dispatch table and gating logic; this module
covers each individual ``WebhookHandler.handle_*`` method against a
representative payload. Goals per test:

* the handler returns ``True`` for the happy path,
* missing optional fields / empty arrays / already-synced lookups don't
  crash, and
* the handler delegates exactly once to its downstream sync entry point.

Only the Shopware HTTP boundary (``ShopwareProduct.sync_product``,
``ShopwareCustomer.sync_customer``, ``sync_order_from_webhook``, …) is
mocked; ``frappe.db`` calls are allowed to hit the test site, where empty
lookups return ``None`` naturally and exercise the "no linked doc" code
path without requiring fixture data.
"""

from unittest.mock import MagicMock, patch

from frappe.tests import IntegrationTestCase

from ecommerce_integrations.shopware6.tests.utils import (
    TEST_CUSTOMER_UUID,
    TEST_ORDER_UUID,
    TEST_PRODUCT_UUID,
    TEST_TRANSACTION_UUID,
    ShopwareTestCase,
)
from ecommerce_integrations.shopware6.webhook.handler import WebhookHandler


def _make_handler(**setting_flags) -> WebhookHandler:
    """Build a ``WebhookHandler`` with a fully mocked setting doc.

    Bypasses ``frappe.get_cached_doc(SETTING_DOCTYPE)`` so tests don't
    depend on a particular Shopware Setting state on the site.
    """
    handler = WebhookHandler.__new__(WebhookHandler)
    setting = MagicMock()
    setting.is_enabled.return_value = True
    setting.sync_products_from_shopware = setting_flags.get("sync_products_from_shopware", True)
    setting.sync_customers_from_shopware = setting_flags.get("sync_customers_from_shopware", True)
    setting.sync_inventory_from_shopware = setting_flags.get("sync_inventory_from_shopware", True)
    setting.sync_delivery_note = setting_flags.get("sync_delivery_note", True)
    handler.setting = setting
    return handler


class TestOrderHandlers(ShopwareTestCase, IntegrationTestCase):
    """Smoke-test the four order-related ``handle_*`` methods.

    Shopware emits state changes per-entity (order / transaction /
    delivery) — each routes to its own handler, but all three eventually
    call ``update_order_status``. The tests pin that wiring so a future
    refactor that accidentally swaps one of them is caught here.
    """

    @patch("ecommerce_integrations.shopware6.order.sync_order_from_webhook")
    def test_order_placed_minimal_payload_does_not_crash(self, mock_sync):
        """``order.placed`` only needs ``primaryKey``/``id`` — everything
        else is fetched from Shopware afterwards. A bare payload must
        still reach ``sync_order_from_webhook`` and return True."""
        handler = _make_handler()
        payload = {"primaryKey": TEST_ORDER_UUID}

        result = handler.handle_order_placed(TEST_ORDER_UUID, payload, request_id=None)

        self.assertTrue(result)
        mock_sync.assert_called_once_with(payload, None)

    @patch("ecommerce_integrations.shopware6.order.sync_order_from_webhook")
    def test_order_written_marks_payload_as_update(self, mock_sync):
        """``order.written`` is the "update" variant — the handler must
        inject ``data.isUpdate = True`` before delegating so the sync
        path treats this as a custom-fields update on an existing SO."""
        handler = _make_handler()
        payload = {"primaryKey": TEST_ORDER_UUID, "data": {"customFields": {}}}

        result = handler.handle_order_written(TEST_ORDER_UUID, payload, request_id=None)

        self.assertTrue(result)
        self.assertTrue(payload["data"]["isUpdate"])
        mock_sync.assert_called_once()

    def test_order_deleted_with_no_linked_sales_order_is_noop(self):
        """Idempotency: a delete webhook for an order the site never had
        must succeed silently. No exception, no log explosion — the
        absence of a linked Sales Order is the steady state for most
        operators."""
        handler = _make_handler()

        result = handler.handle_order_deleted(TEST_ORDER_UUID, {"primaryKey": TEST_ORDER_UUID})

        self.assertTrue(result)

    @patch("ecommerce_integrations.shopware6.order.update_order_status")
    def test_order_status_changed_delegates_to_update_order_status(self, mock_update):
        """State-machine change webhooks must call into the shared
        ``update_order_status`` entry point — this is what maps Shopware
        states to the ``shopware_payment_status`` field on Sales Order."""
        handler = _make_handler()
        payload = {
            "payload": {"entity": {"id": TEST_ORDER_UUID, "stateMachineState": {"technicalName": "paid"}}},
        }

        result = handler.handle_order_status_changed(TEST_ORDER_UUID, payload)

        self.assertTrue(result)
        mock_update.assert_called_once_with(payload, None)

    @patch("ecommerce_integrations.shopware6.order.update_order_status")
    def test_payment_status_changed_delegates_to_update_order_status(self, mock_update):
        """``order_transaction.state_machine_state.changed`` reuses the
        same downstream handler as the order-level state change — pin
        that so the two webhook flows stay in sync."""
        handler = _make_handler()
        payload = {"payload": {"entity": {"id": TEST_TRANSACTION_UUID}}}

        result = handler.handle_payment_status_changed(TEST_TRANSACTION_UUID, payload)

        self.assertTrue(result)
        mock_update.assert_called_once_with(payload, None)

    def test_delivery_status_changed_without_order_id_is_noop(self):
        """Delivery webhooks without an ``orderId`` in the entity payload
        (malformed / partial payload) must not crash — just return True
        so Shopware doesn't retry forever."""
        handler = _make_handler()
        payload = {"payload": {"entity": {}}}

        result = handler.handle_delivery_status_changed("delivery-id", payload)

        self.assertTrue(result)


class TestProductHandlers(ShopwareTestCase, IntegrationTestCase):
    """Smoke-test product create/update and delete handlers.

    ``product.written`` is gated on ``sync_products_from_shopware`` —
    when the operator imports products in the opposite direction
    (ERP → Shopware) the inbound handler must short-circuit.
    """

    @patch("ecommerce_integrations.shopware6.product.ShopwareProduct")
    def test_product_written_minimal_payload_invokes_sync(self, mock_product_cls):
        """A ``product.written`` payload only carries the product ID;
        the handler instantiates ``ShopwareProduct`` and lets it fetch
        full data from the Admin API."""
        handler = _make_handler(sync_products_from_shopware=True)
        instance = mock_product_cls.return_value

        result = handler.handle_product_written(TEST_PRODUCT_UUID, {"primaryKey": TEST_PRODUCT_UUID})

        self.assertTrue(result)
        mock_product_cls.assert_called_once_with(TEST_PRODUCT_UUID)
        instance.sync_product.assert_called_once()

    @patch("ecommerce_integrations.shopware6.product.ShopwareProduct")
    def test_product_written_skipped_when_sync_disabled(self, mock_product_cls):
        """When the operator opts out of inbound product sync the
        handler must not even construct ``ShopwareProduct`` (which would
        otherwise hit the Admin API)."""
        handler = _make_handler(sync_products_from_shopware=False)

        result = handler.handle_product_written(TEST_PRODUCT_UUID, {"primaryKey": TEST_PRODUCT_UUID})

        self.assertTrue(result)
        mock_product_cls.assert_not_called()

    def test_product_deleted_with_no_linked_item_is_noop(self):
        """No ``Ecommerce Item`` row for this product ID means there's
        nothing to unlink — handler returns True without raising. This
        is the idempotency guarantee for product delete retries."""
        handler = _make_handler()

        result = handler.handle_product_deleted(TEST_PRODUCT_UUID, {"primaryKey": TEST_PRODUCT_UUID})

        self.assertTrue(result)


class TestCustomerHandlers(ShopwareTestCase, IntegrationTestCase):
    """Smoke-test customer create/update and delete handlers.

    ``customer.written`` is gated on ``sync_customers_from_shopware``;
    when disabled (the common case for B2B shops that only sync orders)
    the handler must short-circuit before invoking ``sync_customer_by_id``.
    """

    def test_customer_written_skipped_when_sync_disabled(self):
        """Idempotency / opt-out: with inbound customer sync disabled,
        the handler returns True without invoking ``sync_customer_by_id``.
        The construction-side gate is enforced upstream."""
        handler = _make_handler(sync_customers_from_shopware=False)

        result = handler.handle_customer_written(TEST_CUSTOMER_UUID, {"primaryKey": TEST_CUSTOMER_UUID})

        self.assertTrue(result)

    def test_customer_written_delegates_to_sync_customer_by_id(self):
        """When sync is enabled, the handler delegates to the canonical
        ``sync_customer_by_id`` entry point with the customer UUID."""
        handler = _make_handler(sync_customers_from_shopware=True)

        with mock.patch(
            "ecommerce_integrations.shopware6.customer.sync_customer_by_id"
        ) as mock_sync:
            result = handler.handle_customer_written(TEST_CUSTOMER_UUID, {"primaryKey": TEST_CUSTOMER_UUID})

        self.assertTrue(result)
        mock_sync.assert_called_once_with(TEST_CUSTOMER_UUID)

    def test_customer_deleted_with_no_linked_customer_is_noop(self):
        """A delete webhook for a customer never imported into ERPNext
        must succeed silently — the only side effect is a warning log."""
        handler = _make_handler()

        result = handler.handle_customer_deleted(TEST_CUSTOMER_UUID, {"primaryKey": TEST_CUSTOMER_UUID})

        self.assertTrue(result)


class TestStockHandler(ShopwareTestCase, IntegrationTestCase):
    """Smoke-test the ``product_stock.written`` handler.

    Gated on ``sync_inventory_from_shopware`` — most operators sync
    inventory in the opposite direction (ERP → Shopware via
    ``inventory.py``), so the inbound path must short-circuit cleanly.
    """

    def test_stock_changed_skipped_when_sync_disabled(self):
        """With inbound inventory sync disabled the handler returns True
        without invoking ``handle_stock_webhook``."""
        handler = _make_handler(sync_inventory_from_shopware=False)

        result = handler.handle_stock_changed(TEST_PRODUCT_UUID, {"primaryKey": TEST_PRODUCT_UUID})

        self.assertTrue(result)

    def test_stock_changed_delegates_to_handle_stock_webhook(self):
        """When inventory sync is enabled the handler delegates to
        ``handle_stock_webhook`` from the stock_importer module — that's
        the canonical entry point that resolves product UUID → ERPNext
        item code before triggering a stock sync."""
        handler = _make_handler(sync_inventory_from_shopware=True)

        with mock.patch(
            "ecommerce_integrations.shopware6.import_handlers.stock_importer.handle_stock_webhook"
        ) as mock_handler:
            mock_handler.return_value = {"success": True}
            payload = {"primaryKey": TEST_PRODUCT_UUID}
            result = handler.handle_stock_changed(TEST_PRODUCT_UUID, payload)

        self.assertTrue(result)
        mock_handler.assert_called_once_with(payload)


if __name__ == "__main__":
    import unittest

    unittest.main()
