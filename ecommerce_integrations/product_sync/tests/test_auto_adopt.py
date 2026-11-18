"""Adopt existing backend products by SKU, incl. decimal-separator variants."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


class TestAutoAdopt(unittest.TestCase):
    def test_sku_variants(self):
        from ecommerce_integrations.product_sync.api import _sku_variants
        self.assertEqual(_sku_variants("ITEM-001"), ["ITEM-001"])
        self.assertEqual(_sku_variants("956737.5"), ["956737.5", "956737,5"])
        self.assertEqual(_sku_variants("956737,5"), ["956737,5", "956737.5"])

    def test_adopts_comma_spelling(self):
        from ecommerce_integrations.product_sync import api
        adapter = MagicMock()
        adapter.find_matching_products_bulk.return_value = {
            "956737,5": SimpleNamespace(external_id="p-1", sku="956737,5"),
        }
        written = []
        with patch.object(api.frappe, "get_all", return_value=[]), \
             patch.object(api.frappe, "get_cached_doc", side_effect=lambda dt, n: SimpleNamespace(item_code=n, barcodes=[])), \
             patch.object(api, "_write_ecommerce_item", side_effect=lambda *a: written.append(a)), \
             patch("ecommerce_integrations.product_sync.engine.registry.get_adapter", return_value=adapter):
            counts = api.adopt_by_sku(SimpleNamespace(backend="Shopware", name="S"),
                                      item_codes=["956737.5", "ITEM-002"])
        self.assertEqual(counts["adopted"], 1)
        self.assertEqual(counts["no_match"], 1)
        self.assertEqual(written[0][0], "956737.5")
        self.assertEqual(written[0][2], "p-1")
        skus = adapter.find_matching_products_bulk.call_args.kwargs["skus"]
        self.assertIn("956737,5", skus)

    def test_flag_off_skips(self):
        from ecommerce_integrations.product_sync import tasks
        with patch("ecommerce_integrations.product_sync.api.adopt_by_sku") as adopt:
            tasks._auto_adopt(SimpleNamespace(name="S", auto_adopt_by_sku=0), None)
            adopt.assert_not_called()
            tasks._auto_adopt(SimpleNamespace(name="S", auto_adopt_by_sku=1), ["A"])
            adopt.assert_called_once()
