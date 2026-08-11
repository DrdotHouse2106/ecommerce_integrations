"""Tests for ``shopware6.customer.numbering``.

Per CLAUDE.md: real ERPNext DB (no DB mocking) — the counter is a raw
``tabSeries`` row, exactly the kind of thing a mocked DB would hide bugs in.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import frappe
from frappe.tests import IntegrationTestCase

from ecommerce_integrations.shopware6.customer import numbering
from ecommerce_integrations.shopware6.tests.utils import ShopwareTestCase


class TestCustomerNumbering(ShopwareTestCase, IntegrationTestCase):
    def setUp(self):
        super().setUp()
        # Every test gets a clean counter row so tests don't depend on
        # execution order or leak state into each other.
        frappe.db.sql(
            "DELETE FROM `tabSeries` WHERE `name` = %s", (numbering.CUSTOMER_NUMBER_SERIES_KEY,)
        )

    def tearDown(self):
        frappe.db.sql(
            "DELETE FROM `tabSeries` WHERE `name` = %s", (numbering.CUSTOMER_NUMBER_SERIES_KEY,)
        )
        super().tearDown()

    def test_not_initialized_by_default(self):
        self.assertFalse(numbering.is_counter_initialized())

    def test_allocate_before_seed_throws(self):
        with self.assertRaises(frappe.ValidationError):
            numbering.allocate_next_customer_number()

    def test_seed_then_allocate_is_monotonic(self):
        numbering.seed_customer_number_counter(20000)
        self.assertTrue(numbering.is_counter_initialized())
        first = numbering.allocate_next_customer_number()
        second = numbering.allocate_next_customer_number()
        third = numbering.allocate_next_customer_number()
        self.assertEqual([first, second, third], ["20001", "20002", "20003"])

    def test_seed_twice_throws(self):
        numbering.seed_customer_number_counter(1)
        with self.assertRaises(frappe.ValidationError):
            numbering.seed_customer_number_counter(2)

    def test_autoname_noop_without_push_to_shopware(self):
        numbering.seed_customer_number_counter(500)
        doc = MagicMock()
        doc.flags.from_integration = False
        doc.push_to_shopware = False
        doc.name = "should-not-change"
        numbering.autoname_customer_for_shopware_push(doc)
        self.assertEqual(doc.name, "should-not-change")

    def test_autoname_noop_for_integration_originated_docs(self):
        """set_name=-driven inserts (every inbound Shopware/Shopify/Medusa/
        Unicommerce sync) never reach the autoname doc_event at all in
        real Frappe — this guards the fallback path in case that ever
        changes, and documents the ``flags.from_integration`` contract."""
        numbering.seed_customer_number_counter(500)
        doc = MagicMock()
        doc.flags.from_integration = True
        doc.push_to_shopware = True
        doc.name = "shopware-assigned-name"
        numbering.autoname_customer_for_shopware_push(doc)
        self.assertEqual(doc.name, "shopware-assigned-name")

    def test_autoname_allocates_when_opted_in(self):
        numbering.seed_customer_number_counter(500)
        doc = MagicMock()
        doc.flags.from_integration = False
        doc.push_to_shopware = True
        numbering.autoname_customer_for_shopware_push(doc)
        self.assertEqual(doc.name, "501")
