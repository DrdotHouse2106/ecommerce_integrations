"""Universal customField routing.

Two sources feed the Shopware ``customFields`` JSON column for a
product:

1. Operator-configured rows on
   ``Shopware Setting.item_custom_field_mappings`` (one Item DocType
   field → one Shopware customField key, with coercion).
2. ``Item Ecommerce Property`` rows with
   ``property_type = "Custom Field"`` (Shopware key derived from
   ``property_name`` via :func:`shopware_custom_field_name`).

This test exercises Source-2 (the routing decision that lives
inside the universal property table) since Source-1 is covered by
``test_idealo_flag``-style integration tests against the live
Shopware Setting. Both sources land in
``canonical.basic.dynamic_custom_fields`` and merge into the
outgoing payload ``customFields`` dict at apply time.

Critical invariants:

* A property of type "Custom Field" is NOT emitted as a filterable
  property tag (the m2m would create a stray storefront filter
  pill — exactly the bug the routing split fixes).
* A property of type "Property" / "Text" still emits as a filter
  tag and does NOT show up in the customField slot.
* The Shopware key derivation honours
  :func:`shopware_custom_field_name` (incl. unicode preservation
  for umlaut-bearing property names like ``Zubehör``).
* Boolean-string values are coerced via
  :func:`coerce_custom_field_value` so storefront product-stream
  filters work without per-row coercion config.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace


class _Row:
    """Mimics a child-table row enough for the canonical's
    ``getattr(row, …)`` reads."""
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


class _StubItem:
    def __init__(self, item_code: str, ecommerce_properties=None):
        self.item_code = item_code
        self.name = item_code
        self.item_name = "Test"
        self.variant_of = None
        self.disabled = 0
        self.attributes = []
        self.barcodes = []
        self.standard_rate = 0
        self.brand = ""
        self.manufacturer = ""
        self.item_group = ""
        self.ecommerce_properties = ecommerce_properties or []


def _stub_sync(**overrides):
    base = {
        "name": "TEST-SYNC",
        "backend": "Shopware",
        "target_sales_channels": [],
        "sync_pricing": 0,
        "sync_basic_fields": 1,
        "sync_properties": 1,
        "tax_template": None,
        "price_strategy": "item_standard_rate",
        "markup_percent": 0,
        "price_list_override": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class TestDynamicCustomFields(unittest.TestCase):
    def test_custom_field_property_routes_to_customfields_not_properties(self):
        from ecommerce_integrations.product_sync.engine.canonical import (
            _canonical_dynamic_custom_fields,
            _canonical_properties,
        )
        item = _StubItem("ITEM-001", ecommerce_properties=[
            _Row(property_name="Zubehör",
                 property_value="ACC-001,ACC-002",
                 property_type="Custom Field",
                 sync_to_shopware=1),
        ])
        dcf = _canonical_dynamic_custom_fields(item)
        # Source-2 emitted under the derived erpnext_* key with the
        # umlaut preserved (matches the legacy
        # shopware_custom_field_name output).
        self.assertEqual(dcf, {"erpnext_zubehör": "ACC-001,ACC-002"})

        # And the same row must NOT also appear in the filterable
        # properties section — otherwise the storefront would render
        # a stray "Zubehör: ACC-001,ACC-002" filter pill.
        props = _canonical_properties(item, _stub_sync())
        self.assertEqual(props.get("ecommerce_properties"), [])

    def test_property_type_property_stays_in_properties_section(self):
        from ecommerce_integrations.product_sync.engine.canonical import (
            _canonical_dynamic_custom_fields,
            _canonical_properties,
        )
        item = _StubItem("ITEM-002", ecommerce_properties=[
            _Row(property_name="Bereifung",
                 property_value="Luft",
                 property_type="Property",
                 sync_to_shopware=1),
            _Row(property_name="Hinweis",
                 property_value="Abmessungen zusammengeklappt: …",
                 property_type="Text",
                 sync_to_shopware=1),
        ])
        # Filter tags: both Property and Text rows show up.
        props = _canonical_properties(item, _stub_sync())
        ecp = props.get("ecommerce_properties") or []
        names = {p["name"] for p in ecp}
        self.assertIn("Bereifung", names)
        self.assertIn("Hinweis", names)

        # customFields: nothing — neither row is a Custom Field.
        dcf = _canonical_dynamic_custom_fields(item)
        self.assertEqual(dcf, {})

    def test_boolean_coercion_for_custom_field_property(self):
        from ecommerce_integrations.product_sync.engine.canonical import (
            _canonical_dynamic_custom_fields,
        )
        item = _StubItem("ITEM-003", ecommerce_properties=[
            _Row(property_name="is_surcharge",
                 property_value="true",
                 property_type="Custom Field",
                 sync_to_shopware=1),
        ])
        dcf = _canonical_dynamic_custom_fields(item)
        # coerce_custom_field_value recognises "true" as bool True.
        self.assertEqual(dcf, {"erpnext_is_surcharge": True})

    def test_sync_to_shopware_zero_is_skipped(self):
        from ecommerce_integrations.product_sync.engine.canonical import (
            _canonical_dynamic_custom_fields,
        )
        item = _StubItem("ITEM-004", ecommerce_properties=[
            _Row(property_name="Zubehör",
                 property_value="X",
                 property_type="Custom Field",
                 sync_to_shopware=0),
        ])
        self.assertEqual(_canonical_dynamic_custom_fields(item), {})

    def test_youtube_url_migrated_to_property(self):
        """Post-migration shape: YouTube URL lives on
        ``ecommerce_properties`` (property_name "YouTube Video URL",
        type "Custom Field"), no longer hashed under
        ``basic.youtube_video_url``. The derived Shopware key
        ``erpnext_youtube_video_url`` is preserved so the storefront
        block doesn't need to change."""
        from ecommerce_integrations.product_sync.engine.canonical import (
            _canonical_dynamic_custom_fields,
        )
        item = _StubItem("ITEM-005", ecommerce_properties=[
            _Row(property_name="YouTube Video URL",
                 property_value="https://youtu.be/abc123",
                 property_type="Custom Field",
                 sync_to_shopware=1),
        ])
        dcf = _canonical_dynamic_custom_fields(item)
        self.assertEqual(
            dcf,
            {"erpnext_youtube_video_url": "https://youtu.be/abc123"},
        )


if __name__ == "__main__":
    unittest.main()
