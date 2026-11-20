"""Linking a Shopware account to an existing customer needs name AND a known email."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from ecommerce_integrations.shopware6.customer import vat


def _get_all(doctype, filters=None, pluck=None, **kw):
    if doctype == "Customer":
        return ["CUST-001"] if filters["customer_name"] == "Demo Kunde GmbH" else []
    if doctype == "Dynamic Link":
        return ["Kontakt 1"]
    if doctype == "Contact Email":
        return ["Einkauf@Example.com"]
    return []


class TestCustomerLinkEmail(unittest.TestCase):
    def _find(self, email, name="Demo Kunde GmbH"):
        with patch.object(vat.frappe, "get_all", side_effect=_get_all), \
             patch.object(vat.frappe.db, "get_value", return_value="info@example.com"):
            return vat._find_existing_customer_to_link(email=email, company=name, customer_name=name, customer_id="sw-1")

    def test_name_and_known_email_link(self):
        self.assertEqual(self._find("INFO@example.com"), "CUST-001")
        self.assertEqual(self._find(" einkauf@example.com "), "CUST-001")

    def test_name_alone_or_foreign_email_does_not_link(self):
        self.assertIsNone(self._find(""))
        self.assertIsNone(self._find("fremd@example.com"))
        self.assertIsNone(self._find("info@example.com", name="Andere GmbH"))
