"""Tests for ``shopware6.customer.vat._sync_customer_number_to_shopware`` —
the "ERPNext number wins on Shopware" behavior that must run regardless of
``Customer.push_to_shopware`` (see ``customer/push.py`` for the separate,
opt-in-gated *creation* push this must NOT be confused with).

Per CLAUDE.md: real ERPNext DB, mock only the Shopware HTTP client.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils.nestedset import get_root_of

from ecommerce_integrations.shopware6.customer.vat import (
    _find_existing_customer_to_link,
    _sync_customer_number_to_shopware,
)
from ecommerce_integrations.shopware6.tests.utils import ShopwareTestCase


class TestSyncCustomerNumberToShopware(ShopwareTestCase, IntegrationTestCase):
    """Direct tests of the helper — no live Customer doc needed, it only
    takes primitive args and calls the (mocked) client."""

    def test_pushes_when_numbers_differ(self):
        client = MagicMock()
        _sync_customer_number_to_shopware(client, "20001", "shopware-uuid-1", "OLD-NUMBER")
        client.request_patch.assert_called_once_with(
            "customer/shopware-uuid-1", {"customerNumber": "20001"}
        )

    def test_noop_when_already_aligned(self):
        client = MagicMock()
        _sync_customer_number_to_shopware(client, "20001", "shopware-uuid-1", "20001")
        client.request_patch.assert_not_called()

    def test_noop_when_current_number_is_none(self):
        """First-ever alignment: Shopware sent no customerNumber at all
        (e.g. still using its own auto-assigned one) — must still push."""
        client = MagicMock()
        _sync_customer_number_to_shopware(client, "20001", "shopware-uuid-1", None)
        client.request_patch.assert_called_once_with(
            "customer/shopware-uuid-1", {"customerNumber": "20001"}
        )

    def test_noop_without_shopware_customer_id(self):
        client = MagicMock()
        _sync_customer_number_to_shopware(client, "20001", "", "OLD-NUMBER")
        client.request_patch.assert_not_called()

    def test_logs_and_swallows_client_errors(self):
        """A failed PATCH must not raise — this runs inline inside the
        Shopware->ERP webhook handler, one flaky API call must not fail
        the whole customer sync."""
        client = MagicMock()
        client.request_patch.side_effect = Exception("network blip")
        _sync_customer_number_to_shopware(client, "20001", "shopware-uuid-1", "OLD-NUMBER")
        client.request_patch.assert_called_once()


class TestNumberUnificationIgnoresOptInFlag(ShopwareTestCase, IntegrationTestCase):
    """The behavior the user specifically asked about: does a self-
    registered Shopware customer that matches an existing ERPNext customer
    get its number unified regardless of that ERPNext customer's
    push_to_shopware checkbox? Answer must be yes in both cases below."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        territories = frappe.get_all("Territory", limit=1)
        groups = frappe.get_all("Customer Group", limit=1)
        cls.territory = territories[0].name if territories else get_root_of("Territory")
        cls.customer_group = groups[0].name if groups else get_root_of("Customer Group")

    def _make_customer(self, customer_name: str, push_to_shopware: bool) -> str:
        doc = frappe.get_doc(
            {
                "doctype": "Customer",
                "customer_name": customer_name,
                "customer_group": self.customer_group,
                "territory": self.territory,
                "customer_type": "Individual",
                "push_to_shopware": 1 if push_to_shopware else 0,
            }
        )
        doc.insert(ignore_permissions=True)
        self.addCleanup(lambda: frappe.delete_doc("Customer", doc.name, force=True))
        return doc.name

    def test_match_unifies_number_when_opt_in_off(self):
        erp_name = self._make_customer(
            f"Test Unification OptOff {frappe.generate_hash(length=6)}", push_to_shopware=False
        )
        matched = _find_existing_customer_to_link(
            email="",
            company="",
            customer_name=frappe.db.get_value("Customer", erp_name, "customer_name"),
            customer_id="new-shopware-uuid",
        )
        self.assertEqual(matched, erp_name)

        client = MagicMock()
        _sync_customer_number_to_shopware(client, erp_name, "new-shopware-uuid", "SHOPWARE-AUTO-1")
        client.request_patch.assert_called_once_with(
            "customer/new-shopware-uuid", {"customerNumber": erp_name}
        )

    def test_match_unifies_number_when_opt_in_on(self):
        erp_name = self._make_customer(
            f"Test Unification OptOn {frappe.generate_hash(length=6)}", push_to_shopware=True
        )
        matched = _find_existing_customer_to_link(
            email="", company="", customer_name=frappe.db.get_value(
                "Customer", erp_name, "customer_name"
            ), customer_id="new-shopware-uuid-2",
        )
        self.assertEqual(matched, erp_name)

        client = MagicMock()
        _sync_customer_number_to_shopware(client, erp_name, "new-shopware-uuid-2", "SHOPWARE-AUTO-2")
        client.request_patch.assert_called_once_with(
            "customer/new-shopware-uuid-2", {"customerNumber": erp_name}
        )
