"""Verify ``build_shopware_payload`` honours ``changed_sections`` —
only the keys whose source canonical section is in the set may
appear in the payload (plus ``id`` for product identification).
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace


class _StubItem:
    """Minimal duck-type of a Frappe Item doc with the attributes
    ``build_shopware_payload`` reads — keeps the test free of
    frappe.connect overhead.
    """

    def __init__(self, item_code: str, name: str = "Test Item"):
        self.item_code = item_code
        self.item_name = name
        self.variant_of = None
        self.disabled = 0
        self.attributes = []
        self.barcodes = []
        self.standard_rate = 0


def _stub_sync(**overrides):
    # ``sync_pricing=0`` keeps the pricing branch out of these unit
    # tests — pricing pulls Shopware-Setting / currency-map data
    # through ``frappe.cache()`` which needs ``frappe.connect``. The
    # branch is well-covered by integration tests separately.
    base = {
        "name": "TEST-SYNC",
        "backend": "Shopware",
        "target_sales_channels": [],
        "sync_pricing": 0,
        "tax_template": None,
        "price_strategy": "item_standard_rate",
        "markup_percent": 0,
        "price_list_override": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class TestBuildShopwarePayloadPartial(unittest.TestCase):
    def _canonical(self) -> dict:
        return {
            "v": 2,
            "item_code": "ITEM-001",
            "basic": {
                "name": "Test Item",
                "sku": "ITEM-001",
                "description": "<p>Hello</p>",
                "is_active": True,
                "delivery_time": "10-15 Tage",
            },
            "pricing": {"base_price": 19.99, "currency": "EUR"},
            "inventory": {"qty": 7.0},
            "images": [],
            "properties": {"brand": "Acme", "attributes": []},
            "categories": {"ids": []},
        }

    def test_full_payload_when_changed_sections_none(self):
        from ecommerce_integrations.product_sync.engine.payload import (
            build_shopware_payload,
        )
        payload = build_shopware_payload(
            _StubItem("ITEM-001"),
            _stub_sync(),
            self._canonical(),
            external_id="abc",
            changed_sections=None,
        )
        # Full payload has every section key the canonical fed in.
        self.assertEqual(payload.get("id"), "abc")
        self.assertIn("name", payload)
        self.assertIn("productNumber", payload)
        self.assertIn("description", payload)
        self.assertIn("active", payload)
        self.assertIn("stock", payload)
        self.assertIn("deliveryTime", payload)
        self.assertIn("manufacturer", payload)

    def test_basic_only_partial_omits_other_sections(self):
        from ecommerce_integrations.product_sync.engine.payload import (
            build_shopware_payload,
        )
        payload = build_shopware_payload(
            _StubItem("ITEM-001"),
            _stub_sync(),
            self._canonical(),
            external_id="abc",
            changed_sections={"basic"},
        )
        # Basic keys present
        self.assertEqual(payload.get("id"), "abc")
        self.assertIn("name", payload)
        self.assertIn("description", payload)
        self.assertIn("active", payload)
        self.assertIn("deliveryTime", payload)
        # Other sections must NOT be in the payload
        self.assertNotIn("stock", payload)
        self.assertNotIn("price", payload)
        self.assertNotIn("manufacturer", payload)
        self.assertNotIn("categories", payload)

    def test_inventory_only_partial(self):
        from ecommerce_integrations.product_sync.engine.payload import (
            build_shopware_payload,
        )
        payload = build_shopware_payload(
            _StubItem("ITEM-001"),
            _stub_sync(),
            self._canonical(),
            external_id="abc",
            changed_sections={"inventory"},
        )
        self.assertEqual(payload.get("stock"), 7)
        self.assertNotIn("name", payload)
        self.assertNotIn("price", payload)
        self.assertNotIn("manufacturer", payload)

    def test_empty_set_yields_id_only(self):
        """Defensive: an empty changed_sections set means nothing
        drifted — the apply should normally skip the push entirely,
        but if it reaches the builder the result is at most ``{id}``
        (Shopware leaves everything as-is)."""
        from ecommerce_integrations.product_sync.engine.payload import (
            build_shopware_payload,
        )
        payload = build_shopware_payload(
            _StubItem("ITEM-001"),
            _stub_sync(),
            self._canonical(),
            external_id="abc",
            changed_sections=set(),
        )
        self.assertEqual(set(payload.keys()) - {"id"}, set())


if __name__ == "__main__":
    unittest.main()
