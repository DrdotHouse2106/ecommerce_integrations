"""Tests for ``shopware6.import_handlers.customer_number_migration``.

Per CLAUDE.md: real ERPNext DB, mock only the Shopware HTTP client.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils.nestedset import get_root_of

from ecommerce_integrations.shopware6.import_handlers import customer_number_migration as mig
from ecommerce_integrations.shopware6.tests.utils import ShopwareTestCase


def _paged_client(pages: list[list[dict]]):
    """Mock a Shopware client whose ``search/customer`` responses are
    consumed page by page, matching ``_max_numeric_shopware_customer_number``'s
    ``page``/``limit`` loop."""
    client = MagicMock()

    def request_post(path, body):
        resp = MagicMock()
        page = body.get("page", 1)
        resp.data = pages[page - 1] if 0 < page <= len(pages) else []
        return resp

    client.request_post.side_effect = request_post
    return client


class TestMaxNumericShopwareCustomerNumber(ShopwareTestCase, IntegrationTestCase):
    def test_single_page_picks_max_numeric(self):
        client = _paged_client([
            [{"customerNumber": "100"}, {"customerNumber": "20050"}, {"customerNumber": "999"}],
        ])
        self.assertEqual(mig._max_numeric_shopware_customer_number(client), 20050)

    def test_non_numeric_customer_numbers_are_skipped(self):
        client = _paged_client([
            [{"customerNumber": "SW-ABC"}, {"customerNumber": "42"}, {"customerNumber": None}],
        ])
        self.assertEqual(mig._max_numeric_shopware_customer_number(client), 42)

    def test_paginates_across_full_pages(self):
        full_page = [{"customerNumber": str(i)} for i in range(mig._PAGE_SIZE)]
        last_page = [{"customerNumber": "99999"}]
        client = _paged_client([full_page, last_page])
        self.assertEqual(mig._max_numeric_shopware_customer_number(client), 99999)
        self.assertEqual(client.request_post.call_count, 2)

    def test_empty_first_page_returns_zero(self):
        client = _paged_client([[]])
        self.assertEqual(mig._max_numeric_shopware_customer_number(client), 0)

    def test_stops_after_short_page_without_extra_request(self):
        client = _paged_client([[{"customerNumber": "5"}]])  # shorter than _PAGE_SIZE
        mig._max_numeric_shopware_customer_number(client)
        self.assertEqual(client.request_post.call_count, 1)


class TestMaxNumericErpCustomerName(ShopwareTestCase, IntegrationTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        territories = frappe.get_all("Territory", limit=1)
        groups = frappe.get_all("Customer Group", limit=1)
        cls.territory = territories[0].name if territories else get_root_of("Territory")
        cls.customer_group = groups[0].name if groups else get_root_of("Customer Group")

    def _make_customer_with_name(self, name: str) -> None:
        doc = frappe.get_doc(
            {
                "doctype": "Customer",
                "customer_name": f"Migration Test {name}",
                "customer_group": self.customer_group,
                "territory": self.territory,
                "customer_type": "Individual",
            }
        )
        doc.flags.ignore_mandatory = True
        doc.insert(ignore_permissions=True, set_name=name)
        self.addCleanup(lambda: frappe.delete_doc("Customer", name, force=True))

    def test_picks_up_highest_numeric_name(self):
        self._make_customer_with_name("500001")
        self._make_customer_with_name("500042")
        self.assertGreaterEqual(mig._max_numeric_erp_customer_name(), 500042)

    def test_non_numeric_names_are_ignored(self):
        before = mig._max_numeric_erp_customer_name()
        self._make_customer_with_name("Migration Test Non-Numeric GmbH")
        after = mig._max_numeric_erp_customer_name()
        self.assertEqual(before, after)
