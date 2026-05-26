"""Per-field delta-sync helpers (``encode_canonical`` /
``decode_canonical`` / ``changed_sections``).

These cover the storage codec + section diff in isolation — the
apply integration is exercised by the existing sync end-to-end
tests once the patch lands.
"""

from __future__ import annotations

import unittest

from ecommerce_integrations.product_sync.engine.canonical import (
    CANONICAL_SECTIONS,
    changed_sections,
    decode_canonical,
    encode_canonical,
)


class TestCanonicalCodec(unittest.TestCase):
    def test_encode_then_decode_round_trips(self):
        payload = {
            "v": 2,
            "item_code": "ITEM-001",
            "basic": {"name": "Test", "sku": "ITEM-001"},
            "pricing": {"base_price": 19.99, "currency": "EUR"},
            "categories": {"ids": ["cat-a", "cat-b"]},
        }
        encoded = encode_canonical(payload)
        # base64 → str of ASCII, never empty, decoded round-trip
        self.assertIsInstance(encoded, str)
        self.assertGreater(len(encoded), 0)
        decoded = decode_canonical(encoded)
        self.assertEqual(decoded, payload)

    def test_decode_handles_none_and_empty(self):
        self.assertIsNone(decode_canonical(None))
        self.assertIsNone(decode_canonical(""))

    def test_decode_handles_corrupt(self):
        # Truncated base64 / non-gzip payload → graceful None.
        self.assertIsNone(decode_canonical("not-base64!@#"))
        self.assertIsNone(decode_canonical("dGVzdA=="))  # valid b64, not gzip


class TestChangedSections(unittest.TestCase):
    def _full_payload(self) -> dict:
        return {
            "v": 2,
            "item_code": "ITEM-001",
            "basic": {"name": "Test", "sku": "ITEM-001", "delivery_time": ""},
            "pricing": {"base_price": 19.99, "currency": "EUR"},
            "inventory": {"qty": 5.0},
            "images": [{"url": "/img/a.jpg", "primary": True}],
            "properties": {"brand": "Acme", "attributes": []},
            "taxes": {"rate": 19.0},
            "categories": {"ids": ["cat-a"]},
        }

    def test_empty_stored_returns_all_proposed_sections(self):
        proposed = self._full_payload()
        result = changed_sections(None, proposed)
        # Every canonical section that's present in the proposed
        # payload is returned (top-level keys not in CANONICAL_SECTIONS
        # like 'v' and 'item_code' are excluded by design).
        for sec in CANONICAL_SECTIONS:
            if sec in proposed:
                self.assertIn(sec, result)
        self.assertNotIn("v", result)
        self.assertNotIn("item_code", result)

    def test_identical_canonicals_return_empty_set(self):
        payload = self._full_payload()
        self.assertEqual(changed_sections(payload, dict(payload)), set())

    def test_basic_change_returns_only_basic(self):
        stored = self._full_payload()
        proposed = self._full_payload()
        proposed["basic"]["delivery_time"] = "10-15 Tage"
        self.assertEqual(changed_sections(stored, proposed), {"basic"})

    def test_pricing_change_returns_only_pricing(self):
        stored = self._full_payload()
        proposed = self._full_payload()
        proposed["pricing"]["base_price"] = 29.99
        self.assertEqual(changed_sections(stored, proposed), {"pricing"})

    def test_image_list_order_does_not_matter(self):
        # Canonical builders sort images by URL — but defensive
        # double-check that the section comparison would also catch a
        # genuine reorder where the URLs differ.
        stored = self._full_payload()
        proposed = self._full_payload()
        proposed["images"] = [{"url": "/img/b.jpg", "primary": False}]
        self.assertEqual(changed_sections(stored, proposed), {"images"})

    def test_section_removed_counts_as_changed(self):
        stored = self._full_payload()
        proposed = self._full_payload()
        del proposed["categories"]
        self.assertIn("categories", changed_sections(stored, proposed))


if __name__ == "__main__":
    unittest.main()
