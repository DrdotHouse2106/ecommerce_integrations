"""Coalesce properties + variant options into nested associations on
the bulk product upsert.

Verifies:

- ``property_option_uuid`` matches the legacy ``generate_uuid``
  scheme that ``shopware6.export.property_handler`` used for in-place
  creation. Same input → same UUID → idempotent upsert against rows
  already in Shopware.
- ``build_shopware_payload`` emits the ``properties`` m2m on items
  with non-empty ``ecommerce_properties``.
- ``build_shopware_payload`` emits the ``options`` m2m on items with
  ``variant_of`` set, sourced from canonical ``attributes``.
- The two are kept distinct: a value that exists as both a
  filterable property and a variant option gets two different UUIDs
  (matches the dual-scheme the legacy helpers used).
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch


class _StubItem:
    def __init__(self, item_code: str, variant_of: str | None = None):
        self.item_code = item_code
        self.item_name = "Test"
        self.variant_of = variant_of
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


class TestPropertyOptionUuid(unittest.TestCase):
    def test_property_kind_matches_legacy_scheme(self):
        """``property_option_uuid(name, value, kind="property")`` must
        equal what the legacy ``get_or_create_property_option`` would
        have generated. Otherwise upserts would create fresh rows
        instead of matching existing ones."""
        from ecommerce_integrations.product_sync.engine.adapters.shopware import (
            property_option_uuid,
        )
        from ecommerce_integrations.shopware6.export.utils import generate_uuid
        expected = generate_uuid("property_option_Material_Steel")
        self.assertEqual(
            property_option_uuid("Material", "Steel", kind="property"),
            expected,
        )

    def test_variant_kind_matches_legacy_scheme(self):
        from ecommerce_integrations.product_sync.engine.adapters.shopware import (
            property_option_uuid,
        )
        from ecommerce_integrations.shopware6.export.utils import generate_uuid
        expected = generate_uuid("variant_option_Color_Red")
        self.assertEqual(
            property_option_uuid("Color", "Red", kind="variant"),
            expected,
        )

    def test_property_and_variant_kinds_yield_different_uuids(self):
        from ecommerce_integrations.product_sync.engine.adapters.shopware import (
            property_option_uuid,
        )
        self.assertNotEqual(
            property_option_uuid("Material", "Steel", kind="property"),
            property_option_uuid("Material", "Steel", kind="variant"),
        )


class TestBuildShopwarePayloadCoalesce(unittest.TestCase):
    def _canonical(self, *, ecom_props=None, attributes=None) -> dict:
        return {
            "v": 2,
            "item_code": "ITEM-001",
            "basic": {
                "name": "Test", "sku": "ITEM-001",
                "description": "", "is_active": True,
                "delivery_time": "",
            },
            "pricing": {},
            "inventory": {"qty": 0},
            "images": [],
            "properties": {
                "brand": "",
                "manufacturer": "",
                "attributes": attributes or [],
                "ecommerce_properties": ecom_props or [],
            },
            "categories": {"ids": []},
        }

    def test_emits_properties_m2m_when_ecom_props_present(self):
        from ecommerce_integrations.product_sync.engine.payload import (
            build_shopware_payload,
        )
        from ecommerce_integrations.product_sync.engine.adapters.shopware import (
            property_option_uuid,
        )
        canon = self._canonical(ecom_props=[
            {"name": "Material", "value": "Steel"},
            {"name": "Color", "value": "Blue"},
        ])
        payload = build_shopware_payload(
            _StubItem("ITEM-001"), _stub_sync(), canon,
            external_id="abc",
        )
        self.assertIn("properties", payload)
        ids = {p["id"] for p in payload["properties"]}
        self.assertEqual(ids, {
            property_option_uuid("Material", "Steel", kind="property"),
            property_option_uuid("Color", "Blue", kind="property"),
        })

    def test_emits_options_m2m_when_variant_with_attributes(self):
        from ecommerce_integrations.product_sync.engine.payload import (
            build_shopware_payload,
        )
        from ecommerce_integrations.product_sync.engine.adapters.shopware import (
            property_option_uuid,
        )
        canon = self._canonical(attributes=[
            {"name": "Color", "value": "Red"},
            {"name": "Size", "value": "L"},
        ])
        # The payload builder probes ``frappe.db.get_value`` for the
        # template's external_id when ``variant_of`` is set. In a
        # unit-test context ``frappe.local`` isn't bound so the
        # access raises. Mock the ``frappe.db`` attribute on the
        # payload module directly (its ``frappe`` import is what the
        # branch resolves through).
        from ecommerce_integrations.product_sync.engine import payload as _payload_mod
        class _StubDB:
            @staticmethod
            def get_value(*a, **kw):
                return None
        original_frappe = _payload_mod.frappe
        class _StubFrappe:
            db = _StubDB()
        _payload_mod.frappe = _StubFrappe()
        try:
            payload = build_shopware_payload(
                _StubItem("ITEM-001-RED-L", variant_of="ITEM-001-TPL"),
                _stub_sync(), canon, external_id="def",
            )
        finally:
            _payload_mod.frappe = original_frappe
        self.assertIn("options", payload)
        ids = {p["id"] for p in payload["options"]}
        self.assertEqual(ids, {
            property_option_uuid("Color", "Red", kind="variant"),
            property_option_uuid("Size", "L", kind="variant"),
        })

    def test_no_options_on_non_variants(self):
        """Non-variant items must not carry an ``options`` field even
        if canonical's ``attributes`` is populated (Shopware uses
        ``options`` only on variant products)."""
        from ecommerce_integrations.product_sync.engine.payload import (
            build_shopware_payload,
        )
        canon = self._canonical(attributes=[{"name": "Color", "value": "Red"}])
        payload = build_shopware_payload(
            _StubItem("ITEM-002"),
            _stub_sync(), canon, external_id="ghi",
        )
        self.assertNotIn("options", payload)

    def test_partial_payload_drops_properties_when_section_not_changed(self):
        from ecommerce_integrations.product_sync.engine.payload import (
            build_shopware_payload,
        )
        canon = self._canonical(ecom_props=[{"name": "X", "value": "Y"}])
        payload = build_shopware_payload(
            _StubItem("ITEM-001"), _stub_sync(), canon,
            external_id="abc",
            changed_sections={"basic"},  # properties NOT in delta
        )
        self.assertNotIn("properties", payload)
        self.assertNotIn("options", payload)


if __name__ == "__main__":
    unittest.main()
