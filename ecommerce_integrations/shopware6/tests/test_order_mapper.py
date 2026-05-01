"""
Tests for Shopware 6 Order Mapper
"""

import unittest
from unittest.mock import patch, MagicMock

from ecommerce_integrations.shopware6.order.order_mapper import (
    extract_checkout_field_value,
    get_payment_method_info,
    calculate_delivery_date,
    extract_order_currency,
)
from ecommerce_integrations.shopware6.tests.utils import (
    ShopwareTestCase,
    create_sample_order_data,
)


class TestOrderMapper(ShopwareTestCase):
    """Test cases for order mapper functions."""

    def test_extract_checkout_field_from_order(self):
        """customFields on the order itself is the first lookup location."""
        order_data = {
            "customFields": {"custom_po_number": "PO-12345"},
            "orderCustomer": {},
        }
        result = extract_checkout_field_value(order_data, ["custom_po_number"])
        self.assertEqual(result, "PO-12345")

    def test_extract_checkout_field_from_customer(self):
        """Falls back to orderCustomer.customFields when not on the order."""
        order_data = {
            "customFields": {},
            "orderCustomer": {"customFields": {"custom_tel_avis": True}},
        }
        result = extract_checkout_field_value(order_data, ["custom_tel_avis"])
        self.assertEqual(result, True)

    def test_extract_checkout_field_from_top_level(self):
        """Webhook payloads may put fields directly on the order dict."""
        order_data = {"customFields": {}, "orderCustomer": {}, "po_number": "PO-9"}
        result = extract_checkout_field_value(order_data, ["po_number"])
        self.assertEqual(result, "PO-9")

    def test_extract_checkout_field_order_source_beats_customer(self):
        """Order.customFields is consulted before orderCustomer.customFields."""
        order_data = {
            "customFields": {"custom_alt": "from_order"},
            "orderCustomer": {"customFields": {"custom_primary": "from_customer"}},
        }
        result = extract_checkout_field_value(order_data, ["custom_primary", "custom_alt"])
        self.assertEqual(result, "from_order")

    def test_extract_checkout_field_not_found(self):
        order_data = {"customFields": {}, "orderCustomer": {}}
        result = extract_checkout_field_value(order_data, ["po_number"])
        self.assertIsNone(result)

    def test_get_payment_method_info_with_transaction(self):
        """Test extracting payment method from transaction."""
        order_data = create_sample_order_data()

        method_name, erpnext_mode, status = get_payment_method_info(order_data)

        self.assertEqual(method_name, "Invoice")
        self.assertEqual(status, "Unpaid")  # "open" maps to "Unpaid"

    def test_get_payment_method_info_no_transactions(self):
        """Test payment method info with no transactions."""
        order_data = {"transactions": []}

        method_name, erpnext_mode, status = get_payment_method_info(order_data)

        self.assertIsNone(method_name)
        self.assertEqual(status, "Unpaid")

    def test_extract_order_currency(self):
        """Test extracting currency from order."""
        order_data = create_sample_order_data()

        currency = extract_order_currency(order_data)
        self.assertEqual(currency, "EUR")

    def test_extract_order_currency_default(self):
        """Test currency defaults to EUR."""
        order_data = {}

        currency = extract_order_currency(order_data)
        self.assertEqual(currency, "EUR")

    @patch("ecommerce_integrations.shopware6.order.order_mapper.frappe")
    @patch("ecommerce_integrations.shopware6.order.order_mapper.get_item_code")
    def test_calculate_delivery_date(self, mock_get_item_code, mock_frappe):
        """Test delivery date calculation based on lead time."""
        mock_get_item_code.return_value = "TEST-ITEM"
        mock_frappe.db.get_value.return_value = 5  # 5 days lead time

        order_date = "2024-01-15"
        line_items = [
            {"productId": "product-1", "payload": {"productNumber": "TEST-ITEM"}}
        ]

        result = calculate_delivery_date(order_date, line_items)

        # Should be order_date + 5 days
        self.assertEqual(result, "2024-01-20")

    @patch("ecommerce_integrations.shopware6.order.order_mapper.frappe")
    @patch("ecommerce_integrations.shopware6.order.order_mapper.get_item_code")
    def test_calculate_delivery_date_minimum(self, mock_get_item_code, mock_frappe):
        """Test delivery date has minimum 1 day lead time."""
        mock_get_item_code.return_value = "TEST-ITEM"
        mock_frappe.db.get_value.return_value = 0  # No lead time

        order_date = "2024-01-15"
        line_items = [
            {"productId": "product-1", "payload": {"productNumber": "TEST-ITEM"}}
        ]

        result = calculate_delivery_date(order_date, line_items)

        # Should be order_date + 1 day (minimum)
        self.assertEqual(result, "2024-01-16")


if __name__ == "__main__":
    unittest.main()
