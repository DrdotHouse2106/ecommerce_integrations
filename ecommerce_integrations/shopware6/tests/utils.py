"""
Shopware 6 Test Utilities

Base classes and helper functions for Shopware 6 integration tests.
Follows the Shopify test pattern.
"""

import json
import os
from typing import Any, Dict, Optional
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ecommerce_integrations.shopware6.constants import SETTING_DOCTYPE


class ShopwareTestCase(FrappeTestCase):
    """
    Base test case for Shopware 6 integration tests.

    Provides common setup, teardown, and helper methods.
    """

    @classmethod
    def setUpClass(cls):
        """Set up test fixtures."""
        super().setUpClass()
        cls.setup_test_settings()

    @classmethod
    def setup_test_settings(cls):
        """Create test Shopware settings."""
        if not frappe.db.exists(SETTING_DOCTYPE, SETTING_DOCTYPE):
            # Create minimal test settings
            setting = frappe.get_doc({
                "doctype": SETTING_DOCTYPE,
                "enable_shopware": 0,  # Disabled for tests
                "shopware_url": "https://test.example.com",
                "api_key": "test_api_key",
                "api_secret": "test_api_secret",
                "company": frappe.get_all("Company", limit=1)[0].name if frappe.get_all("Company", limit=1) else None,
            })
            setting.insert(ignore_permissions=True)
        else:
            # Ensure integration is disabled for tests
            frappe.db.set_value(SETTING_DOCTYPE, SETTING_DOCTYPE, "enable_shopware", 0)

    def get_test_data(self, filename: str) -> Dict[str, Any]:
        """
        Load test data from JSON file.

        Args:
            filename: Name of the JSON file in tests/data/

        Returns:
            Parsed JSON data
        """
        data_path = os.path.join(
            os.path.dirname(__file__),
            "data",
            filename
        )
        with open(data_path, "r") as f:
            return json.load(f)

    def create_mock_client(self) -> MagicMock:
        """
        Create a mock Shopware API client.

        Returns:
            MagicMock configured as Shopware client
        """
        client = MagicMock()
        client.request_get.return_value = {"data": {}}
        client.request_post.return_value = {"data": []}
        client.request_patch.return_value = {"data": {}}
        client.request_delete.return_value = {}
        return client

    def create_test_item(
        self,
        item_code: str = None,
        item_name: str = None,
        **kwargs
    ) -> "frappe.Document":
        """
        Create a test Item document.

        Args:
            item_code: Item code (auto-generated if not provided)
            item_name: Item name (defaults to item_code)
            **kwargs: Additional item fields

        Returns:
            Created Item document
        """
        if not item_code:
            item_code = f"TEST-{frappe.generate_hash(length=8)}"

        if not item_name:
            item_name = item_code

        item = frappe.get_doc({
            "doctype": "Item",
            "item_code": item_code,
            "item_name": item_name,
            "item_group": "Products" if frappe.db.exists("Item Group", "Products") else "All Item Groups",
            "stock_uom": "Nos",
            "is_stock_item": 1,
            **kwargs
        })
        item.insert(ignore_permissions=True)
        return item

    def create_test_customer(
        self,
        customer_name: str = None,
        **kwargs
    ) -> "frappe.Document":
        """
        Create a test Customer document.

        Args:
            customer_name: Customer name (auto-generated if not provided)
            **kwargs: Additional customer fields

        Returns:
            Created Customer document
        """
        if not customer_name:
            customer_name = f"Test Customer {frappe.generate_hash(length=6)}"

        customer = frappe.get_doc({
            "doctype": "Customer",
            "customer_name": customer_name,
            "customer_type": "Individual",
            "customer_group": "Commercial" if frappe.db.exists("Customer Group", "Commercial") else "All Customer Groups",
            "territory": "Germany" if frappe.db.exists("Territory", "Germany") else "All Territories",
            **kwargs
        })
        customer.insert(ignore_permissions=True)
        return customer

    def cleanup_test_documents(self, doctype: str, filters: Dict = None):
        """
        Delete test documents matching filters.

        Args:
            doctype: DocType to clean up
            filters: Filters for deletion
        """
        if filters is None:
            filters = {"name": ["like", "TEST-%"]}

        docs = frappe.get_all(doctype, filters=filters, pluck="name")
        for doc in docs:
            try:
                frappe.delete_doc(doctype, doc, force=True, ignore_permissions=True)
            except Exception:
                pass


def mock_shopware_session(func):
    """
    Decorator to mock the Shopware session decorator.

    Use this instead of @temp_shopware_session in tests.
    """
    def wrapper(*args, **kwargs):
        # Create a mock client
        client = MagicMock()
        client.request_get.return_value = {"data": {}}
        client.request_post.return_value = {"data": []}
        client.request_patch.return_value = {"data": {}}
        client.request_delete.return_value = {}

        # Call the function with mock client
        return func(client, *args, **kwargs)

    return wrapper


def create_sample_order_data() -> Dict[str, Any]:
    """Create sample Shopware order data for testing."""
    return {
        "id": "test-order-id-123",
        "orderNumber": "10001",
        "orderDateTime": "2024-01-15T10:30:00.000+00:00",
        "orderCustomer": {
            "id": "test-customer-id-456",
            "customerId": "test-customer-id-456",
            "email": "test@example.com",
            "firstName": "Max",
            "lastName": "Mustermann",
            "salutation": {"salutationKey": "mr", "displayName": "Herr"},
        },
        "billingAddress": {
            "id": "test-address-id-789",
            "firstName": "Max",
            "lastName": "Mustermann",
            "street": "Musterstraße 123",
            "city": "Berlin",
            "zipcode": "10115",
            "country": {"iso": "DE", "name": "Germany"},
        },
        "lineItems": [
            {
                "id": "line-item-1",
                "type": "product",
                "productId": "product-id-123",
                "label": "Test Product",
                "quantity": 2,
                "unitPrice": 49.99,
                "totalPrice": 99.98,
                "price": {
                    "calculatedTaxes": [
                        {"taxRate": 19.0, "tax": 15.96}
                    ]
                },
                "payload": {
                    "productNumber": "TEST-001"
                }
            }
        ],
        "transactions": [
            {
                "id": "transaction-id-1",
                "paymentMethod": {"name": "Invoice", "shortName": "invoice"},
                "stateMachineState": {"technicalName": "open"}
            }
        ],
        "deliveries": [],
        "currency": {"isoCode": "EUR"},
        "customFields": {},
    }


def create_sample_product_data() -> Dict[str, Any]:
    """Create sample Shopware product data for testing."""
    return {
        "id": "test-product-id-123",
        "productNumber": "TEST-001",
        "name": "Test Product",
        "description": "A test product for testing",
        "active": True,
        "stock": 100,
        "price": [
            {
                "currencyId": "currency-eur-id",
                "gross": 59.99,
                "net": 50.41,
                "linked": False
            }
        ],
        "taxId": "tax-19-id",
        "categories": [],
        "customFields": {},
    }


def create_sample_customer_data() -> Dict[str, Any]:
    """Create sample Shopware customer data for testing."""
    return {
        "id": "test-customer-id-456",
        "email": "test@example.com",
        "firstName": "Max",
        "lastName": "Mustermann",
        "company": "",
        "accountType": "private",
        "salutation": {"salutationKey": "mr", "displayName": "Herr"},
        "defaultBillingAddress": {
            "id": "billing-address-id",
            "firstName": "Max",
            "lastName": "Mustermann",
            "street": "Musterstraße 123",
            "city": "Berlin",
            "zipcode": "10115",
            "country": {"iso": "DE", "name": "Germany"},
            "countryState": None,
        },
        "defaultShippingAddress": {
            "id": "shipping-address-id",
            "firstName": "Max",
            "lastName": "Mustermann",
            "street": "Musterstraße 123",
            "city": "Berlin",
            "zipcode": "10115",
            "country": {"iso": "DE", "name": "Germany"},
            "countryState": None,
        },
        "vatIds": [],
    }
