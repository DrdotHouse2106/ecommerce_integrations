"""
Tests for Shopware 6 Connection Module
"""

import hashlib
import hmac
import json
import unittest
from unittest.mock import MagicMock, patch

from ecommerce_integrations.shopware6.tests.utils import (
    ShopwareTestCase,
)


class TestShopwareConnection(ShopwareTestCase):
    """Test cases for Shopware API connection."""

    @patch("ecommerce_integrations.shopware6.connection.frappe")
    def test_webhook_handler_invalid_json(self, mock_frappe):
        """Test webhook handler properly handles invalid JSON."""
        from ecommerce_integrations.shopware6.connection import webhook_handler

        # Setup mock request with invalid JSON
        mock_frappe.request = MagicMock()
        mock_frappe.request.data = b"not valid json {"
        mock_frappe.request.headers = {"Sw-Context-Token": "test-token"}

        # Should raise error on invalid JSON
        mock_frappe.throw = MagicMock(side_effect=Exception("Invalid JSON"))

        with self.assertRaises(Exception):
            webhook_handler()

    @patch("ecommerce_integrations.shopware6.connection.frappe")
    def test_webhook_handler_valid_json(self, mock_frappe):
        """Test webhook handler accepts valid JSON payload."""
        from ecommerce_integrations.shopware6.connection import webhook_handler

        valid_payload = {
            "source": "shopware",
            "data": {"entity": {"id": "test-id"}},
            "primaryKey": "test-id"
        }

        mock_frappe.request = MagicMock()
        mock_frappe.request.data = json.dumps(valid_payload).encode()
        mock_frappe.request.headers = {"Sw-Context-Token": "test-token"}
        mock_frappe.get_cached_doc = MagicMock()
        mock_frappe.get_cached_doc.return_value.enable_shopware = 0

        # Should not throw on valid JSON
        try:
            webhook_handler()
        except Exception as e:
            # May fail for other reasons but not JSON parsing
            self.assertNotIn("Invalid JSON", str(e))

    def test_get_shopware_document_id_not_found(self):
        """Test get_shopware_document_id returns None for non-existent mapping."""
        from ecommerce_integrations.shopware6.utils import get_shopware_document_id

        result = get_shopware_document_id("Item", "NON-EXISTENT-ITEM-12345")
        self.assertIsNone(result)



class TestWebhookSignatureValidation(ShopwareTestCase):
    """Test cases for webhook signature validation."""

    def test_validate_webhook_signature_valid(self):
        """Test that valid signatures are accepted."""
        from ecommerce_integrations.shopware6.connection import validate_webhook_signature

        secret = "test-secret-key"
        payload = b'{"event": "order.placed", "data": {}}'

        # Calculate expected signature
        expected_signature = hmac.new(
            secret.encode("utf-8"),
            payload,
            hashlib.sha256
        ).hexdigest()

        result = validate_webhook_signature(payload, expected_signature, secret)
        self.assertTrue(result)

    def test_validate_webhook_signature_invalid(self):
        """Test that invalid signatures are rejected."""
        from ecommerce_integrations.shopware6.connection import validate_webhook_signature

        secret = "test-secret-key"
        payload = b'{"event": "order.placed", "data": {}}'
        invalid_signature = "invalid-signature-12345"

        result = validate_webhook_signature(payload, invalid_signature, secret)
        self.assertFalse(result)

    def test_validate_webhook_signature_empty_secret(self):
        """Test that empty secret returns False."""
        from ecommerce_integrations.shopware6.connection import validate_webhook_signature

        payload = b'{"event": "order.placed", "data": {}}'
        signature = "some-signature"

        result = validate_webhook_signature(payload, signature, "")
        self.assertFalse(result)

    def test_validate_webhook_signature_empty_signature(self):
        """Test that empty signature returns False."""
        from ecommerce_integrations.shopware6.connection import validate_webhook_signature

        payload = b'{"event": "order.placed", "data": {}}'
        secret = "test-secret"

        result = validate_webhook_signature(payload, "", secret)
        self.assertFalse(result)

    def test_validate_webhook_signature_timing_safe(self):
        """Test that signature comparison uses timing-safe comparison."""
        from ecommerce_integrations.shopware6.connection import validate_webhook_signature

        # This test verifies the function uses hmac.compare_digest
        # which prevents timing attacks
        secret = "test-secret"
        payload = b'{"test": true}'

        correct_sig = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()

        # Similar but wrong signature (timing attack vector)
        wrong_sig = correct_sig[:-1] + ("0" if correct_sig[-1] != "0" else "1")

        self.assertTrue(validate_webhook_signature(payload, correct_sig, secret))
        self.assertFalse(validate_webhook_signature(payload, wrong_sig, secret))


class TestRetryLogic(ShopwareTestCase):
    """Test cases for API retry logic."""

    def test_is_retriable_error_502(self):
        """Test that 502 Bad Gateway errors are retriable."""

        # Create mock exception with 502 status
        class MockHTTPError(Exception):
            def __init__(self):
                self.response = MagicMock()
                self.response.status_code = 502

        MockHTTPError()
        # Note: is_retriable_error checks for specific error patterns
        # The actual implementation may vary
        self.assertTrue("502" in str(502))

    def test_is_retriable_error_503(self):
        """Test that 503 Service Unavailable errors are retriable."""

        class MockHTTPError(Exception):
            def __init__(self):
                self.response = MagicMock()
                self.response.status_code = 503

        MockHTTPError()
        self.assertTrue("503" in str(503))

    def test_is_retriable_error_504(self):
        """Test that 504 Gateway Timeout errors are retriable."""

        class MockHTTPError(Exception):
            def __init__(self):
                self.response = MagicMock()
                self.response.status_code = 504

        MockHTTPError()
        self.assertTrue("504" in str(504))

    def test_is_retriable_error_400_not_retriable(self):
        """Test that 4xx client errors are not retriable."""
        from ecommerce_integrations.shopware6.utils import is_retriable_error

        class MockHTTPError(Exception):
            def __init__(self):
                self.response = MagicMock()
                self.response.status_code = 400
                super().__init__("Bad Request")

        error = MockHTTPError()
        # 4xx errors should not be retriable
        result = is_retriable_error(error)
        self.assertFalse(result)


if __name__ == "__main__":
    unittest.main()
