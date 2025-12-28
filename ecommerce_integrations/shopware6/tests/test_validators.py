"""
Tests for Shopware 6 Data Validators
"""

import unittest
from ecommerce_integrations.shopware6.validators import (
    ShopwareDataValidator,
    validate_and_log,
)
from ecommerce_integrations.shopware6.tests.utils import (
    ShopwareTestCase,
    create_sample_order_data,
    create_sample_product_data,
    create_sample_customer_data,
)


class TestShopwareDataValidator(ShopwareTestCase):
    """Test cases for ShopwareDataValidator."""

    def test_validate_order_valid(self):
        """Test validation of valid order data."""
        order_data = create_sample_order_data()
        is_valid, errors = ShopwareDataValidator.validate_order(order_data)

        self.assertTrue(is_valid)
        self.assertEqual(len(errors), 0)

    def test_validate_order_missing_id(self):
        """Test validation fails for missing order ID."""
        order_data = create_sample_order_data()
        del order_data["id"]

        is_valid, errors = ShopwareDataValidator.validate_order(order_data)

        self.assertFalse(is_valid)
        self.assertIn("Missing required field: id", errors)

    def test_validate_order_missing_order_customer(self):
        """Test validation fails for missing orderCustomer."""
        order_data = create_sample_order_data()
        del order_data["orderCustomer"]

        is_valid, errors = ShopwareDataValidator.validate_order(order_data)

        self.assertFalse(is_valid)
        self.assertIn("Missing required field: orderCustomer", errors)

    def test_validate_order_empty(self):
        """Test validation fails for empty order data."""
        is_valid, errors = ShopwareDataValidator.validate_order({})

        self.assertFalse(is_valid)
        self.assertTrue(len(errors) > 0)

    def test_validate_order_none(self):
        """Test validation fails for None order data."""
        is_valid, errors = ShopwareDataValidator.validate_order(None)

        self.assertFalse(is_valid)
        self.assertIn("Order data is empty", errors)

    def test_validate_customer_valid(self):
        """Test validation of valid customer data."""
        customer_data = create_sample_customer_data()
        is_valid, errors = ShopwareDataValidator.validate_customer(customer_data)

        self.assertTrue(is_valid)
        self.assertEqual(len(errors), 0)

    def test_validate_customer_missing_email(self):
        """Test validation fails for missing email."""
        customer_data = create_sample_customer_data()
        del customer_data["email"]

        is_valid, errors = ShopwareDataValidator.validate_customer(customer_data)

        self.assertFalse(is_valid)
        self.assertIn("Missing required field: email", errors)

    def test_validate_customer_invalid_email(self):
        """Test validation fails for invalid email format."""
        customer_data = create_sample_customer_data()
        customer_data["email"] = "not-an-email"

        is_valid, errors = ShopwareDataValidator.validate_customer(customer_data)

        self.assertFalse(is_valid)
        self.assertTrue(any("Invalid email format" in e for e in errors))

    def test_validate_product_valid(self):
        """Test validation of valid product data."""
        product_data = create_sample_product_data()
        is_valid, errors = ShopwareDataValidator.validate_product(product_data)

        self.assertTrue(is_valid)
        self.assertEqual(len(errors), 0)

    def test_validate_product_missing_product_number(self):
        """Test validation fails for missing productNumber."""
        product_data = create_sample_product_data()
        del product_data["productNumber"]

        is_valid, errors = ShopwareDataValidator.validate_product(product_data)

        self.assertFalse(is_valid)
        self.assertIn("Missing required field: productNumber", errors)

    def test_validate_webhook_payload_valid(self):
        """Test validation of valid webhook payload."""
        payload = {
            "source": "shopware",
            "data": {"orderId": "test-id"},
            "primaryKey": "test-id"
        }
        is_valid, errors = ShopwareDataValidator.validate_webhook_payload(payload)

        self.assertTrue(is_valid)
        self.assertEqual(len(errors), 0)

    def test_validate_webhook_payload_no_entity_id(self):
        """Test validation fails when no entity ID found."""
        payload = {"source": "shopware", "data": {}}
        is_valid, errors = ShopwareDataValidator.validate_webhook_payload(payload)

        self.assertFalse(is_valid)
        self.assertIn("No entity ID found in webhook payload", errors)

    def test_is_valid_uuid(self):
        """Test UUID validation."""
        # Valid UUIDs
        self.assertTrue(ShopwareDataValidator._is_valid_uuid("550e8400e29b41d4a716446655440000"))
        self.assertTrue(ShopwareDataValidator._is_valid_uuid("550e8400-e29b-41d4-a716-446655440000"))

        # Invalid UUIDs
        self.assertFalse(ShopwareDataValidator._is_valid_uuid("not-a-uuid"))
        self.assertFalse(ShopwareDataValidator._is_valid_uuid("123"))

    def test_is_valid_email(self):
        """Test email validation."""
        # Valid emails
        self.assertTrue(ShopwareDataValidator._is_valid_email("test@example.com"))
        self.assertTrue(ShopwareDataValidator._is_valid_email("user.name+tag@example.co.uk"))

        # Invalid emails
        self.assertFalse(ShopwareDataValidator._is_valid_email("not-an-email"))
        self.assertFalse(ShopwareDataValidator._is_valid_email("missing@domain"))

    def test_validate_line_item_product(self):
        """Test validation of product line item."""
        line_item = {
            "type": "product",
            "productId": "product-id-123",
            "quantity": 2,
        }
        is_valid, errors = ShopwareDataValidator.validate_line_item(line_item)

        self.assertTrue(is_valid)
        self.assertEqual(len(errors), 0)

    def test_validate_line_item_missing_product_id(self):
        """Test validation fails for product without productId."""
        line_item = {
            "type": "product",
            "quantity": 2,
        }
        is_valid, errors = ShopwareDataValidator.validate_line_item(line_item)

        self.assertFalse(is_valid)
        self.assertTrue(any("productId" in e for e in errors))


if __name__ == "__main__":
    unittest.main()
