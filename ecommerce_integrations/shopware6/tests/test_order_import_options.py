"""Order import: sales-channel filter, plugin decimal quantities and
product numbers with the other decimal separator."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


def _setting(**kw):
    base = {"order_import_channel_filter": 0, "sales_channels": [], "decimal_quantity_payload_path": "",
            "warehouse": "Stores - X", "add_shipping_as_item": 0}
    base.update(kw)
    return SimpleNamespace(**base)


class TestChannelFilter(unittest.TestCase):
    def test_filter_off_allows_all(self):
        from ecommerce_integrations.shopware6.order.order_sync import order_channel_allowed
        self.assertTrue(order_channel_allowed({"salesChannelId": "x"}, _setting()))

    def test_filter_on_only_marked_channels(self):
        from ecommerce_integrations.shopware6.order.order_sync import order_channel_allowed
        rows = [SimpleNamespace(sales_channel_id="a", import_orders=1),
                SimpleNamespace(sales_channel_id="b", import_orders=0)]
        s = _setting(order_import_channel_filter=1, sales_channels=rows)
        self.assertTrue(order_channel_allowed({"salesChannelId": "a"}, s))
        self.assertFalse(order_channel_allowed({"salesChannelId": "b"}, s))
        self.assertFalse(order_channel_allowed({"salesChannelId": "c"}, s))


class TestDecimalQuantity(unittest.TestCase):
    def test_path_lookup(self):
        from ecommerce_integrations.shopware6.order.line_item_handler import decimal_quantity
        line = {"payload": {"plugin": {"value": "2,5"}}}
        self.assertEqual(decimal_quantity(line, _setting(decimal_quantity_payload_path="plugin.value")), 2.5)
        self.assertEqual(decimal_quantity(line, _setting()), 0.0)
        self.assertEqual(decimal_quantity(line, _setting(decimal_quantity_payload_path="plugin.missing")), 0.0)

    def test_line_gets_decimal_qty_and_rate(self):
        from ecommerce_integrations.shopware6.order import line_item_handler as h
        so = MagicMock(delivery_date="2026-11-18")
        line = {"type": "product", "quantity": 1, "unitPrice": 119.70, "totalPrice": 119.70,
                "label": "Dämmmatte", "payload": {"plugin": {"value": 3}}}
        with patch.object(h, "flt", lambda v, *a: float(v or 0)), \
             patch.object(h, "get_item_code", return_value="ITEM-001"), \
             patch.object(h, "_ensure_sales_item"), \
             patch.object(h.frappe.db, "get_value", return_value=None):
            h.add_order_item(so, line, _setting(decimal_quantity_payload_path="plugin.value"), "gross")
        row = so.append.call_args.args[1]
        self.assertEqual(row["qty"], 3)
        self.assertAlmostEqual(row["rate"], 39.90)

    def test_piece_mode_uses_total_not_times_quantity(self):
        # 4 boards of 0.5 m² each: Shopware quantity 4, payload total 2 m².
        from ecommerce_integrations.shopware6.order import line_item_handler as h
        so = MagicMock(delivery_date="2026-11-18")
        line = {"type": "product", "quantity": 4, "unitPrice": 20.0, "totalPrice": 80.0,
                "label": "Platte", "payload": {"plugin": {"value": 2}}}
        with patch.object(h, "flt", lambda v, *a: float(v or 0)), \
             patch.object(h, "get_item_code", return_value="ITEM-001"), \
             patch.object(h, "_ensure_sales_item"), \
             patch.object(h.frappe.db, "get_value", return_value=None):
            h.add_order_item(so, line, _setting(decimal_quantity_payload_path="plugin.value"), "gross")
        row = so.append.call_args.args[1]
        self.assertEqual(row["qty"], 2)
        self.assertAlmostEqual(row["rate"], 40.0)


class TestSkuVariants(unittest.TestCase):
    def test_variants(self):
        from ecommerce_integrations.utils.sku import sku_variants
        self.assertEqual(sku_variants("956737,5"), ["956737,5", "956737.5"])
        self.assertEqual(sku_variants("ITEM-001"), ["ITEM-001"])
        self.assertEqual(sku_variants(""), [])

    def test_get_item_code_falls_back_to_dot_spelling(self):
        from ecommerce_integrations.shopware6 import product
        with patch.object(product.ecommerce_item, "get_erpnext_item", return_value=None), \
             patch.object(product.frappe.db, "exists", side_effect=lambda dt, n: n == "956737.5"):
            code = product.get_item_code({"productId": "p", "payload": {"productNumber": "956737,5"}})
        self.assertEqual(code, "956737.5")


class TestTaxTemplate(unittest.TestCase):
    def _tpl(self, order, **kw):
        from ecommerce_integrations.shopware6.order import order_mapper as m
        setting = SimpleNamespace(**{"tax_template_domestic_gross": "Inland inkl - X",
                                     "tax_template_domestic_net": "Inland zzgl - X",
                                     "tax_template_eu_b2b": "EU B2B - X", **kw})
        with patch.object(m, "get_delivery_country_code", return_value=order.pop("_country")), \
             patch.object(m, "has_valid_vat_id", return_value=order.pop("_vat", False)), \
             patch.object(m.frappe.db, "get_value", return_value="X"), \
             patch.object(m.frappe.db, "exists", return_value=False):
            return m.get_tax_template(order, "Test GmbH", setting)

    def test_domestic_by_tax_status(self):
        self.assertEqual(self._tpl({"_country": "DE", "taxStatus": "gross"}), "Inland inkl - X")
        self.assertEqual(self._tpl({"_country": "DE", "taxStatus": "net"}), "Inland zzgl - X")
        self.assertEqual(self._tpl({"_country": "DE", "price": {"taxStatus": "net"}}), "Inland zzgl - X")

    def test_eu_and_unconfigured(self):
        self.assertEqual(self._tpl({"_country": "AT", "_vat": True}), "EU B2B - X")
        self.assertEqual(self._tpl({"_country": "US"}), "")  # not configured, built-in name missing
