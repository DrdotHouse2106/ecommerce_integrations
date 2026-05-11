"""
Tests for Shopware 6 Order Mapper
"""

import unittest
from unittest.mock import patch

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

        # ``payment_method_name`` prefers Shopware's ``shortName`` over the
        # display name, because the configurable mapping in Shopware Setting
        # keys on the short name (stable, language-independent).
        self.assertEqual(method_name, "invoice")
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
        import frappe

        mock_get_item_code.return_value = "TEST-ITEM"
        # ``frappe.db.get_value(..., as_dict=True)`` returns a ``frappe._dict``
        # which supports both ``.get()`` and attribute access. The mapper
        # uses attribute access on the fallback path, so a plain dict won't
        # do — use _dict directly.
        mock_frappe.db.get_value.return_value = frappe._dict(
            delivery_time=None, lead_time_days=5,
        )

        order_date = "2024-01-15"
        line_items = [
            {"productId": "product-1", "payload": {"productNumber": "TEST-ITEM"}}
        ]

        # 5 days > the default 1-day floor → result is order_date + 5
        result = calculate_delivery_date(order_date, line_items)
        self.assertEqual(result, "2024-01-20")

    @patch("ecommerce_integrations.shopware6.order.order_mapper.frappe")
    @patch("ecommerce_integrations.shopware6.order.order_mapper.get_item_code")
    def test_calculate_delivery_date_minimum(self, mock_get_item_code, mock_frappe):
        """Test delivery date has minimum 1 day lead time (no same-day delivery)."""
        import frappe

        mock_get_item_code.return_value = "TEST-ITEM"
        # Both unset → item falls through to default 1-day floor.
        mock_frappe.db.get_value.return_value = frappe._dict(
            delivery_time=None, lead_time_days=0,
        )

        order_date = "2024-01-15"
        line_items = [
            {"productId": "product-1", "payload": {"productNumber": "TEST-ITEM"}}
        ]

        # Should be order_date + 1 day (minimum floor in calculate_delivery_date)
        result = calculate_delivery_date(order_date, line_items)
        self.assertEqual(result, "2024-01-16")


if __name__ == "__main__":
    unittest.main()
