"""Stock only for flagged items and optionally from one warehouse subtree."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch


class TestInventoryScope(unittest.TestCase):
    def setUp(self):
        from ecommerce_integrations.product_sync.engine import canonical
        self.c = canonical
        __import__("frappe").local._product_sync_memo = {}

    def test_no_flag_means_all_items(self):
        self.assertTrue(self.c._inventory_enabled_for(SimpleNamespace(item_code="A"), SimpleNamespace()))

    def test_flag_limits_items(self):
        import frappe
        with patch.object(frappe, "get_all", return_value=["A"]), \
             patch.object(frappe, "get_meta", return_value=SimpleNamespace(has_field=lambda f: True)):
            sync = SimpleNamespace(inventory_item_flag="custom_transfer_stock")
            self.assertTrue(self.c._inventory_enabled_for(SimpleNamespace(item_code="A"), sync))
            self.assertFalse(self.c._inventory_enabled_for(SimpleNamespace(item_code="B"), sync))

    def test_warehouse_subtree(self):
        import frappe
        with patch.object(frappe.db, "sql", return_value=[("A", 4.0)]):
            sync = SimpleNamespace(inventory_warehouse="Stores - X")
            self.assertEqual(self.c._canonical_inventory(SimpleNamespace(item_code="A"), sync)["qty"], 4.0)
            self.assertEqual(self.c._canonical_inventory(SimpleNamespace(item_code="B"), sync)["qty"], 0.0)

    def test_payload_without_inventory_section_sends_no_stock(self):
        from ecommerce_integrations.product_sync.engine.payload import build_shopware_payload
        from ecommerce_integrations.product_sync.tests.test_payload_partial import (
            TestBuildShopwarePayloadPartial, _StubItem, _stub_sync,
        )
        canonical = TestBuildShopwarePayloadPartial._canonical(None)
        canonical.pop("inventory")
        payload = build_shopware_payload(_StubItem("ITEM-001"), _stub_sync(), canonical, external_id="x")
        self.assertNotIn("stock", payload)
