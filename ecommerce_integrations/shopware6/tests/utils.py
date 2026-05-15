"""Shopware 6 Test Utilities — base test class and sample-data factories."""

from typing import Any
from unittest.mock import MagicMock

import frappe
from frappe.tests.utils import FrappeTestCase

from ecommerce_integrations.shopware6.constants import SETTING_DOCTYPE

# Deterministic 32-char hex UUIDs used by validator and dispatch tests.
# The slot numbers carry no meaning beyond making collisions obvious in
# debug output (``…010001`` = order, ``…020001`` = customer, etc.).
TEST_ORDER_UUID = "00000000000000000000000000010001"
TEST_CUSTOMER_UUID = "00000000000000000000000000020001"
TEST_BILLING_ADDRESS_UUID = "00000000000000000000000000030001"
TEST_SHIPPING_ADDRESS_UUID = "00000000000000000000000000030002"
TEST_LINE_ITEM_UUID = "00000000000000000000000000040001"
TEST_PRODUCT_UUID = "00000000000000000000000000050001"
TEST_TRANSACTION_UUID = "00000000000000000000000000060001"


class ShopwareTestCase(FrappeTestCase):
    """Base test case for Shopware 6 integration tests.

    Ensures Shopware Setting exists and is disabled (so accidentally-live
    sync calls in tests short-circuit) before any test runs.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.setup_test_settings()

    @classmethod
    def setup_test_settings(cls):
        """Create or disable the Shopware Setting so tests can't hit the API."""
        if not frappe.db.exists(SETTING_DOCTYPE, SETTING_DOCTYPE):
            companies = frappe.get_all("Company", limit=1)
            setting = frappe.get_doc({
                "doctype": SETTING_DOCTYPE,
                "enable_shopware": 0,
                "shopware_url": "https://test.example.com",
                "api_key": "test_api_key",
                "api_secret": "test_api_secret",
                "company": companies[0].name if companies else None,
            })
            setting.insert(ignore_permissions=True)
        else:
            frappe.db.set_value(SETTING_DOCTYPE, SETTING_DOCTYPE, "enable_shopware", 0)

    @staticmethod
    def make_enabled_setting_mock(**attrs) -> MagicMock:
        """Build a Shopware Setting mock that reports the integration enabled.

        ``@patch("…frappe").get_cached_doc.return_value = make_enabled_setting_mock()``
        is the shape the dispatch / init tests need; consolidating this
        boilerplate avoids ~30 lines of copy-paste across the test modules.
        Pass additional attributes (e.g. ``order_sync_frequency=60``) as kwargs.
        """
        m = MagicMock()
        m.is_enabled.return_value = True
        for k, v in attrs.items():
            setattr(m, k, v)
        return m


def create_sample_order_data() -> dict[str, Any]:
    """Create sample Shopware order data for testing.

    All IDs use the ``TEST_*_UUID`` constants above — 32-char hex strings
    that match what ``ShopwareDataValidator._is_valid_uuid`` accepts.
    """
    return {
        "id": TEST_ORDER_UUID,
        "orderNumber": "10001",
        "orderDateTime": "2024-01-15T10:30:00.000+00:00",
        "orderCustomer": {
            "id": TEST_CUSTOMER_UUID,
            "customerId": TEST_CUSTOMER_UUID,
            "email": "kunde@example.com",
            "firstName": "Demo",
            "lastName": "Kunde",
            "salutation": {"salutationKey": "mr", "displayName": "Herr"},
        },
        "billingAddress": {
            "id": TEST_BILLING_ADDRESS_UUID,
            "firstName": "Demo",
            "lastName": "Kunde",
            "street": "Musterstraße 1",
            "city": "Berlin",
            "zipcode": "10115",
            "country": {"iso": "DE", "name": "Germany"},
        },
        "lineItems": [
            {
                "id": TEST_LINE_ITEM_UUID,
                "type": "product",
                "productId": TEST_PRODUCT_UUID,
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
                "id": TEST_TRANSACTION_UUID,
                "paymentMethod": {"name": "Invoice", "shortName": "invoice"},
                "stateMachineState": {"technicalName": "open"}
            }
        ],
        "deliveries": [],
        "currency": {"isoCode": "EUR"},
        "customFields": {},
    }


def create_sample_product_data() -> dict[str, Any]:
    """Create sample Shopware product data for testing."""
    return {
        "id": TEST_PRODUCT_UUID,
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


def create_sample_customer_data() -> dict[str, Any]:
    """Create sample Shopware customer data for testing."""
    return {
        "id": TEST_CUSTOMER_UUID,
        "email": "kunde@example.com",
        "firstName": "Demo",
        "lastName": "Kunde",
        "company": "",
        "accountType": "private",
        "salutation": {"salutationKey": "mr", "displayName": "Herr"},
        "defaultBillingAddress": {
            "id": TEST_BILLING_ADDRESS_UUID,
            "firstName": "Demo",
            "lastName": "Kunde",
            "street": "Musterstraße 1",
            "city": "Berlin",
            "zipcode": "10115",
            "country": {"iso": "DE", "name": "Germany"},
            "countryState": None,
        },
        "defaultShippingAddress": {
            "id": TEST_SHIPPING_ADDRESS_UUID,
            "firstName": "Demo",
            "lastName": "Kunde",
            "street": "Musterstraße 1",
            "city": "Berlin",
            "zipcode": "10115",
            "country": {"iso": "DE", "name": "Germany"},
            "countryState": None,
        },
        "vatIds": [],
    }
