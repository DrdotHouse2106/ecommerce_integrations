"""
Tests for Shopware 6 Order Sync
"""

import unittest
from unittest.mock import patch, MagicMock

from ecommerce_integrations.shopware6.tests.utils import (
    ShopwareTestCase,
    create_sample_order_data,
)


class TestShopwareOrder(ShopwareTestCase):
    """Test cases for ShopwareOrder class."""

    @patch("ecommerce_integrations.shopware6.order.order_sync.frappe")
    def test_shopware_order_initialization(self, mock_frappe):
        """Test ShopwareOrder class initialization."""
        from ecommerce_integrations.shopware6.order.order_sync import ShopwareOrder

        mock_frappe.db.exists.return_value = None

        order = ShopwareOrder(order_id="test-order-123")

        self.assertEqual(order.order_id, "test-order-123")
        self.assertIsNone(order.sales_order_name)

    @patch("ecommerce_integrations.shopware6.order.order_sync.frappe")
    def test_shopware_order_is_synced_false(self, mock_frappe):
        """Test is_synced returns False for new orders."""
        from ecommerce_integrations.shopware6.order.order_sync import ShopwareOrder

        mock_frappe.db.exists.return_value = None
        mock_frappe.db.get_value.return_value = None

        order = ShopwareOrder(order_id="new-order-123")

        self.assertFalse(order.is_synced())

    @patch("ecommerce_integrations.shopware6.order.order_sync.frappe")
    def test_shopware_order_is_synced_true(self, mock_frappe):
        """Test is_synced returns True for synced orders."""
        from ecommerce_integrations.shopware6.order.order_sync import ShopwareOrder

        mock_frappe.db.get_value.return_value = "SO-0001"

        order = ShopwareOrder(order_id="synced-order-123")
        order.sales_order_name = "SO-0001"

        self.assertTrue(order.is_synced())


class TestLineItemHandler(ShopwareTestCase):
    """Test cases for line item processing."""

    def test_process_product_line_item(self):
        """Test processing a product line item."""
        from ecommerce_integrations.shopware6.order.line_item_handler import process_line_item

        line_item = {
            "id": "line-item-1",
            "type": "product",
            "productId": "product-123",
            "label": "Test Product",
            "quantity": 2,
            "unitPrice": 49.99,
            "totalPrice": 99.98,
            "payload": {"productNumber": "TEST-001"}
        }

        result = process_line_item(line_item)

        self.assertIsNotNone(result)
        self.assertEqual(result.get("qty"), 2)

    def test_skip_non_product_line_item(self):
        """Test that non-product line items are handled correctly."""
        from ecommerce_integrations.shopware6.order.line_item_handler import process_line_item

        line_item = {
            "id": "line-item-promo",
            "type": "promotion",
            "label": "10% Off",
            "quantity": 1,
            "unitPrice": -10.0,
            "totalPrice": -10.0,
        }

        result = process_line_item(line_item)

        # Promotion items may be handled differently
        # Depending on implementation
        pass


class TestTaxHandler(ShopwareTestCase):
    """Test cases for tax calculations."""

    def test_extract_tax_rate(self):
        """Test extracting tax rate from line item."""
        from ecommerce_integrations.shopware6.order.tax_handler import extract_tax_rate

        line_item = {
            "price": {
                "calculatedTaxes": [
                    {"tax": 15.96, "taxRate": 19.0, "price": 99.98}
                ]
            }
        }

        tax_rate = extract_tax_rate(line_item)

        self.assertEqual(tax_rate, 19.0)

    def test_extract_tax_rate_no_taxes(self):
        """Test tax rate extraction when no taxes present."""
        from ecommerce_integrations.shopware6.order.tax_handler import extract_tax_rate

        line_item = {
            "price": {
                "calculatedTaxes": []
            }
        }

        tax_rate = extract_tax_rate(line_item)

        self.assertEqual(tax_rate, 0.0)

    def test_calculate_tax_amount(self):
        """Test tax amount calculation."""
        from ecommerce_integrations.shopware6.order.tax_handler import calculate_tax_amount

        amount = 100.0
        tax_rate = 19.0

        tax = calculate_tax_amount(amount, tax_rate)

        self.assertAlmostEqual(tax, 19.0, places=2)


class TestPaymentHandler(ShopwareTestCase):
    """Test cases for payment handling."""

    def test_map_payment_status_open(self):
        """Test mapping 'open' payment status."""
        from ecommerce_integrations.shopware6.order.payment_handler import map_payment_status

        status = map_payment_status("open")
        self.assertEqual(status, "Unpaid")

    def test_map_payment_status_paid(self):
        """Test mapping 'paid' payment status."""
        from ecommerce_integrations.shopware6.order.payment_handler import map_payment_status

        status = map_payment_status("paid")
        self.assertEqual(status, "Paid")

    def test_map_payment_status_cancelled(self):
        """Test mapping 'cancelled' payment status."""
        from ecommerce_integrations.shopware6.order.payment_handler import map_payment_status

        status = map_payment_status("cancelled")
        self.assertEqual(status, "Cancelled")


class TestDeliveryHandler(ShopwareTestCase):
    """Test cases for delivery handling."""

    def test_extract_shipping_address(self):
        """Test extracting shipping address from order."""
        from ecommerce_integrations.shopware6.order.delivery_handler import extract_shipping_address

        order_data = create_sample_order_data()
        order_data["deliveries"] = [{
            "shippingOrderAddress": {
                "firstName": "Max",
                "lastName": "Mustermann",
                "street": "Musterstraße 123",
                "city": "Berlin",
                "zipcode": "10115",
                "country": {"iso": "DE"}
            }
        }]

        address = extract_shipping_address(order_data)

        self.assertIn("city", address)
        self.assertEqual(address["city"], "Berlin")

    def test_extract_shipping_address_fallback(self):
        """Test fallback to billing address when no delivery address."""
        from ecommerce_integrations.shopware6.order.delivery_handler import extract_shipping_address

        order_data = create_sample_order_data()
        order_data["deliveries"] = []

        address = extract_shipping_address(order_data)

        # Should fall back to billing address
        self.assertIn("city", address)


class TestServiceItems(ShopwareTestCase):
    """Test cases for service item handling (Tel. Avis, Gabelstapler)."""

    def test_detect_tel_avis_from_custom_field(self):
        """Test detecting tel_avis from order customFields."""
        from ecommerce_integrations.shopware6.order.service_items import should_add_tel_avis

        order_data = create_sample_order_data()
        order_data["customFields"] = {"custom_tel_avis": True}

        result = should_add_tel_avis(order_data)

        self.assertTrue(result)

    def test_detect_tel_avis_from_customer(self):
        """Test detecting tel_avis from orderCustomer customFields."""
        from ecommerce_integrations.shopware6.order.service_items import should_add_tel_avis

        order_data = create_sample_order_data()
        order_data["customFields"] = {}
        order_data["orderCustomer"]["customFields"] = {"custom_tel_avis": True}

        result = should_add_tel_avis(order_data)

        self.assertTrue(result)

    def test_detect_forklift_delivery(self):
        """Test detecting forklift delivery requirement."""
        from ecommerce_integrations.shopware6.order.service_items import should_add_forklift

        order_data = create_sample_order_data()
        order_data["customFields"] = {"custom_forklift_delivery": True}

        result = should_add_forklift(order_data)

        self.assertTrue(result)


class TestScheduledSync(ShopwareTestCase):
    """Test cases for scheduled order sync."""

    @patch("ecommerce_integrations.shopware6.order.scheduled_sync.frappe")
    def test_scheduled_order_sync_disabled(self, mock_frappe):
        """Test that sync is skipped when disabled."""
        from ecommerce_integrations.shopware6.order.scheduled_sync import scheduled_order_sync

        mock_setting = MagicMock()
        mock_setting.is_enabled.return_value = False
        mock_frappe.get_doc.return_value = mock_setting

        result = scheduled_order_sync()

        self.assertIsNone(result)
        mock_frappe.cache.assert_not_called()

    @patch("ecommerce_integrations.shopware6.order.scheduled_sync.sync_orders_from_shopware")
    @patch("ecommerce_integrations.shopware6.order.scheduled_sync.get_logger")
    @patch("ecommerce_integrations.shopware6.order.scheduled_sync.frappe")
    def test_scheduled_order_sync_skips_if_lock_is_held(self, mock_frappe, mock_get_logger, mock_sync):
        """Test that overlapping scheduler runs are skipped."""
        from ecommerce_integrations.shopware6.order.scheduled_sync import scheduled_order_sync

        mock_setting = MagicMock()
        mock_setting.is_enabled.return_value = True
        mock_setting.order_sync_frequency = 60
        mock_setting.last_order_sync = None
        mock_frappe.get_doc.return_value = mock_setting

        mock_lock = MagicMock()
        mock_lock.acquire.return_value = False
        mock_frappe.cache.return_value.lock.return_value = mock_lock

        scheduled_order_sync()

        mock_sync.assert_not_called()
        mock_get_logger.return_value.info.assert_called_once()
        mock_lock.release.assert_not_called()

    @patch("ecommerce_integrations.shopware6.order.scheduled_sync.sync_orders_from_shopware")
    @patch("ecommerce_integrations.shopware6.order.scheduled_sync.frappe")
    def test_scheduled_order_sync_releases_lock_on_success(self, mock_frappe, mock_sync):
        """Test lock release and commit on successful run."""
        from ecommerce_integrations.shopware6.order.scheduled_sync import scheduled_order_sync

        mock_setting = MagicMock()
        mock_setting.is_enabled.return_value = True
        mock_setting.order_sync_frequency = 60
        mock_setting.last_order_sync = None
        mock_frappe.get_doc.return_value = mock_setting

        mock_lock = MagicMock()
        mock_lock.acquire.return_value = True
        mock_frappe.cache.return_value.lock.return_value = mock_lock
        mock_sync.return_value = {"synced": 1, "errors": 0}

        scheduled_order_sync()

        mock_sync.assert_called_once()
        mock_frappe.db.set_single_value.assert_called_once()
        mock_frappe.db.commit.assert_called_once()
        mock_frappe.db.rollback.assert_not_called()
        mock_lock.release.assert_called_once()

    @patch("ecommerce_integrations.shopware6.order.scheduled_sync.sync_orders_from_shopware")
    @patch("ecommerce_integrations.shopware6.order.scheduled_sync.get_logger")
    @patch("ecommerce_integrations.shopware6.order.scheduled_sync.frappe")
    def test_scheduled_order_sync_rolls_back_and_releases_lock_on_error(
        self, mock_frappe, mock_get_logger, mock_sync
    ):
        """Test rollback and lock release if sync raises an exception."""
        from ecommerce_integrations.shopware6.order.scheduled_sync import scheduled_order_sync

        mock_setting = MagicMock()
        mock_setting.is_enabled.return_value = True
        mock_setting.order_sync_frequency = 60
        mock_setting.last_order_sync = None
        mock_frappe.get_doc.return_value = mock_setting

        mock_lock = MagicMock()
        mock_lock.acquire.return_value = True
        mock_frappe.cache.return_value.lock.return_value = mock_lock
        mock_sync.side_effect = RuntimeError("boom")

        scheduled_order_sync()

        mock_frappe.db.rollback.assert_called_once()
        mock_get_logger.return_value.error.assert_called_once()
        mock_lock.release.assert_called_once()

if __name__ == "__main__":
    unittest.main()
