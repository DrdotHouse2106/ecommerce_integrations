"""Order lines carry the item tax template of their Shopware rate."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from ecommerce_integrations.shopware6.order import tax_handler as th

DE_ROWS = [SimpleNamespace(account_head="UST19", rate=0), SimpleNamespace(account_head="UST7", rate=0)]
OSS_ROWS = [SimpleNamespace(account_head="OSS", rate=20)]


def _setting():
    return SimpleNamespace(company="Test GmbH", cost_center=None, default_sales_tax_account="UST19",
                           taxes=[SimpleNamespace(shopware_tax=19, tax_account="UST19"),
                                  SimpleNamespace(shopware_tax=7, tax_account="UST7")])


def _get_all(rows):
    def fake(doctype, filters=None, fields=None, pluck=None, **kw):
        if doctype == "Sales Taxes and Charges":
            return rows
        if doctype == "Item Tax Template Detail":
            return [f"{filters['tax_rate']:g} % {filters['tax_type']}"]
        if doctype == "Item Tax Template":
            return filters["name"][1]
        return []
    return fake


class TestTaxLines(unittest.TestCase):
    def setUp(self):
        p = patch.object(th, "flt", side_effect=lambda v, *a: float(v or 0))
        p.start()
        self.addCleanup(p.stop)

    def test_rate_parts(self):
        self.assertEqual(th.rate_parts([{"taxRate": 19, "price": 5}], -5), [(19.0, -5.0)])
        self.assertEqual(th.rate_parts([], 4.9), [(None, 4.9)])
        self.assertEqual(th.rate_parts([{"taxRate": 19, "price": 6}, {"taxRate": 7, "price": 4}], -10),
                         [(19.0, -6.0), (7.0, -4.0)])

    def test_domestic_rates_use_setting_accounts(self):
        with patch.object(th.frappe, "get_all", side_effect=_get_all(DE_ROWS)):
            self.assertEqual(th.item_tax_template_for_rate(7, _setting(), "Inland"), "7 % UST7")
            self.assertEqual(th.account_for_rate(19, _setting(), "Inland"), "UST19")

    def test_single_account_template_takes_every_rate(self):
        with patch.object(th.frappe, "get_all", side_effect=_get_all(OSS_ROWS)):
            self.assertEqual(th.account_for_rate(10, _setting(), "OSS AT"), "OSS")
            self.assertEqual(th.item_tax_template_for_rate(10, _setting(), "OSS AT"), "10 % OSS")

    def test_one_tax_row_per_account(self):
        so = SimpleNamespace(rows=[], get=lambda k: "OSS AT")
        so.append = lambda table, row: so.rows.append(row)
        order = {"lineItems": [{"price": {"calculatedTaxes": [{"taxRate": 20, "tax": 2}]}},
                               {"price": {"calculatedTaxes": [{"taxRate": 10, "tax": 1}]}}]}
        with patch.object(th.frappe, "get_all", side_effect=_get_all(OSS_ROWS)):
            th.add_order_taxes(so, order, _setting(), "gross")
        self.assertEqual([(r["account_head"], r["rate"]) for r in so.rows], [("OSS", 20.0)])
