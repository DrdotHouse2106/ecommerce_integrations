"""Tests for ``shopware6.customer.push`` — the opt-in ERPNext-to-Shopware
customer creation push.

Per CLAUDE.md: real ERPNext DB, mock only the Shopware HTTP client /
``frappe.enqueue`` (queueing is itself an external side effect we don't
want to actually schedule during a unit test).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from ecommerce_integrations.shopware6.customer import push
from ecommerce_integrations.shopware6.tests.utils import ShopwareTestCase


def _doc(**overrides):
    doc = MagicMock()
    doc.flags.from_integration = False
    doc.name = "20001"
    doc.push_to_shopware = False
    doc.shopware_customer_id = None
    for k, v in overrides.items():
        setattr(doc, k, v)
    return doc


class TestQueueCustomerForSync(ShopwareTestCase, IntegrationTestCase):
    def _enabled_setting(self, enable_customer_push=True):
        m = MagicMock()
        m.is_enabled.return_value = True
        m.enable_customer_push = enable_customer_push
        return m

    @patch("frappe.enqueue")
    def test_noop_when_from_integration(self, mock_enqueue):
        doc = _doc(push_to_shopware=True)
        doc.flags.from_integration = True
        push.queue_customer_for_sync(doc)
        mock_enqueue.assert_not_called()

    @patch("frappe.enqueue")
    def test_noop_when_opt_in_off(self, mock_enqueue):
        with patch("frappe.get_cached_doc", return_value=self._enabled_setting()):
            push.queue_customer_for_sync(_doc(push_to_shopware=False))
        mock_enqueue.assert_not_called()

    @patch("frappe.enqueue")
    def test_noop_when_customer_push_disabled_on_setting(self, mock_enqueue):
        with patch(
            "frappe.get_cached_doc", return_value=self._enabled_setting(enable_customer_push=False)
        ):
            push.queue_customer_for_sync(_doc(push_to_shopware=True))
        mock_enqueue.assert_not_called()

    @patch("frappe.enqueue")
    def test_noop_when_already_linked(self, mock_enqueue):
        with patch("frappe.get_cached_doc", return_value=self._enabled_setting()):
            push.queue_customer_for_sync(
                _doc(push_to_shopware=True, shopware_customer_id="already-linked-uuid")
            )
        mock_enqueue.assert_not_called()

    @patch("frappe.enqueue")
    def test_enqueues_with_expected_job_id_when_eligible(self, mock_enqueue):
        with patch("frappe.get_cached_doc", return_value=self._enabled_setting()):
            push.queue_customer_for_sync(_doc(push_to_shopware=True, name="20001"))
        mock_enqueue.assert_called_once()
        _, kwargs = mock_enqueue.call_args
        self.assertEqual(kwargs["customer_name"], "20001")
        self.assertEqual(kwargs["job_id"], "shopware_customer_push:20001")
        self.assertTrue(kwargs["deduplicate"])
        self.assertTrue(kwargs["enqueue_after_commit"])


class TestBuildCustomerPayload(ShopwareTestCase, IntegrationTestCase):
    def _setting(self):
        # Unique names per call: resolve_shopware_customer_group_id/etc. are
        # Redis-cached by name (shopware6.base.cache_manager), which
        # persists across test methods — a fixed name would let an earlier
        # test's successful resolution mask a later test's "not found" case.
        suffix = frappe.generate_hash(length=8)
        return SimpleNamespace(
            sales_channels=[
                SimpleNamespace(is_default=False, sales_channel_id="sc-other"),
                SimpleNamespace(is_default=True, sales_channel_id="sc-default"),
            ],
            shopware_default_customer_group_name=f"Standard-{suffix}",
            shopware_default_payment_method_name=f"Invoice-{suffix}",
            shopware_default_salutation_key=f"not_specified-{suffix}",
        )

    def _customer(self, **overrides):
        c = SimpleNamespace(
            name=f"no-contact-{frappe.generate_hash(length=8)}",
            customer_name="Demo Kunde",
            customer_type="Individual",
            email_id="kunde@example.com",
        )
        for k, v in overrides.items():
            setattr(c, k, v)
        return c

    def _client(self, group_id="group-1", payment_id="pay-1", salutation_id="sal-1"):
        client = MagicMock()

        def request_post(path, body):
            resp = MagicMock()
            if path == "search/customer-group":
                resp.data = [{"id": group_id}]
            elif path == "search/payment-method":
                resp.data = [{"id": payment_id}]
            elif path == "search/salutation":
                resp.data = [{"id": salutation_id}]
            else:
                resp.data = []
            return resp

        client.request_post.side_effect = request_post
        return client

    def test_payload_has_no_guest_flag_and_no_password(self):
        payload = push._build_customer_payload(self._client(), self._customer(), self._setting())
        self.assertNotIn("guest", payload)
        self.assertNotIn("password", payload)

    def test_payload_uses_default_sales_channel_and_erp_customer_number(self):
        customer = self._customer(name="20042")
        payload = push._build_customer_payload(self._client(), customer, self._setting())
        self.assertEqual(payload["salesChannelId"], "sc-default")
        self.assertEqual(payload["customerNumber"], "20042")
        self.assertEqual(payload["email"], "kunde@example.com")

    def test_payload_falls_back_to_placeholder_email(self):
        customer = self._customer(name="20043", email_id="")
        payload = push._build_customer_payload(self._client(), customer, self._setting())
        self.assertEqual(payload["email"], "20043@placeholder.invalid")

    def test_payload_sets_company_only_for_company_customers(self):
        individual = self._customer(customer_type="Individual", customer_name="Max Mustermann")
        company = self._customer(customer_type="Company", customer_name="Test GmbH")
        payload_individual = push._build_customer_payload(self._client(), individual, self._setting())
        payload_company = push._build_customer_payload(self._client(), company, self._setting())
        self.assertNotIn("company", payload_individual)
        self.assertEqual(payload_company["company"], "Test GmbH")

    def test_throws_when_no_default_sales_channel(self):
        setting = self._setting()
        setting.sales_channels = [SimpleNamespace(is_default=False, sales_channel_id="sc-other")]
        with self.assertRaises(frappe.ValidationError):
            push._build_customer_payload(self._client(), self._customer(), setting)

    def test_throws_when_customer_group_not_found(self):
        client = self._client()
        client.request_post.side_effect = lambda path, body: MagicMock(
            data=[] if path == "search/customer-group" else [{"id": "x"}]
        )
        with self.assertRaises(frappe.ValidationError):
            push._build_customer_payload(client, self._customer(), self._setting())
