"""Brand → Shopware ``product_manufacturer`` UUID + payload nesting.

The legacy ``shopware6.export.product_mapper.get_or_create_manufacturer``
helper produced a deterministic UUID via
``generate_uuid(f"manufacturer_{name}")``. The new bulk path
(``ensure_brand_entities_bulk``) AND the nested ``manufacturer.id``
the product upsert emits must match that scheme — otherwise the
product upsert would link to a parallel "stub" manufacturer row
without the logo + description that the bulk path just enriched.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace


class _StubItem:
    def __init__(self, item_code: str):
        self.item_code = item_code
        self.item_name = "Test"
        self.variant_of = None
        self.disabled = 0
        self.attributes = []
        self.barcodes = []
        self.standard_rate = 0


def _stub_sync(**overrides):
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


class TestManufacturerUuid(unittest.TestCase):
    def test_payload_uses_legacy_manufacturer_uuid(self):
        """The product payload's nested manufacturer must reference
        the same id ``generate_uuid("manufacturer_<name>")`` produces,
        otherwise the bulk enrichment never lands on the row the
        product links to."""
        from ecommerce_integrations.product_sync.engine.payload import (
            build_shopware_payload,
        )
        from ecommerce_integrations.shopware6.export.utils import generate_uuid
        canon = {
            "v": 2, "item_code": "ITEM-001",
            "basic": {
                "name": "T", "sku": "ITEM-001", "description": "",
                "is_active": True, "delivery_time": "",
            },
            "pricing": {}, "inventory": {"qty": 0}, "images": [],
            "properties": {
                "brand": "Test GmbH",
                "manufacturer": "",
                "attributes": [],
                "ecommerce_properties": [],
            },
            "categories": {"ids": []},
        }
        payload = build_shopware_payload(
            _StubItem("ITEM-001"), _stub_sync(), canon, external_id="ext",
        )
        self.assertEqual(
            payload["manufacturer"]["id"],
            generate_uuid("manufacturer_Test GmbH"),
        )
        self.assertEqual(payload["manufacturer"]["name"], "Test GmbH")

    def test_no_manufacturer_field_when_brand_empty(self):
        from ecommerce_integrations.product_sync.engine.payload import (
            build_shopware_payload,
        )
        canon = {
            "v": 2, "item_code": "ITEM-001",
            "basic": {"name": "T", "sku": "ITEM-001", "description": "",
                      "is_active": True, "delivery_time": ""},
            "pricing": {}, "inventory": {"qty": 0}, "images": [],
            "properties": {"brand": "", "manufacturer": "",
                           "attributes": [], "ecommerce_properties": []},
            "categories": {"ids": []},
        }
        payload = build_shopware_payload(
            _StubItem("ITEM-002"), _stub_sync(), canon, external_id="ext",
        )
        self.assertNotIn("manufacturer", payload)


if __name__ == "__main__":
    unittest.main()
