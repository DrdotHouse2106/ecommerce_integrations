"""Existing Shopware property groups/options are reused by name instead
of creating same-named twins with deterministic UUIDs."""

from __future__ import annotations

import unittest
from unittest.mock import patch


class TestPropertyAdoption(unittest.TestCase):
    def setUp(self):
        from ecommerce_integrations.product_sync.engine.adapters import shopware as sw
        self.sw = sw
        self.addCleanup(sw._remember_existing_properties, [], [])
        sw._remember_existing_properties(
            [{"id": "g-legacy", "name": "Teileart"}, {"id": "g-color", "name": "Farbe"}],
            [{"id": "o-1", "name": "Bremse", "groupId": "g-legacy"},
             {"id": "o-2", "name": "Rot", "groupId": "g-color"}],
        )

    def test_existing_ids_are_reused_case_insensitive(self):
        self.assertEqual(self.sw.property_group_uuid("teileart "), "g-legacy")
        self.assertEqual(self.sw.property_option_uuid("Teileart", "bremse"), "o-1")
        self.assertEqual(self.sw.property_option_uuid("Farbe", "Rot", kind="variant"), "o-2")

    def test_unknown_values_fall_back_to_deterministic_uuid(self):
        from ecommerce_integrations.shopware6.export.utils import generate_uuid
        self.assertEqual(self.sw.property_option_uuid("Teileart", "Motor"),
                         generate_uuid("property_option_Teileart_Motor"))
        self.assertEqual(self.sw.property_group_uuid("Material"), generate_uuid("property_group_Material"))

    def test_deterministic_group_wins_over_twin(self):
        from ecommerce_integrations.shopware6.export.utils import generate_uuid
        det = generate_uuid("property_group_Teileart")
        self.sw._remember_existing_properties(
            [{"id": "g-legacy", "name": "Teileart"}, {"id": det, "name": "Teileart"}], [])
        self.assertEqual(self.sw.property_group_uuid("Teileart"), det)

    def test_ensure_options_skips_existing_and_uses_adopted_group(self):
        sent = []

        class Client:
            def request_post(self, path, payload=None):
                sent.append(payload)

        with patch.object(self.sw, "_with_client", lambda fn: fn(Client())), \
             patch("ecommerce_integrations.product_sync.engine.canonical._load_property_catalog", return_value={}):
            self.sw.ShopwareProductAdapter.ensure_property_options_bulk(
                None, [("Teileart", "Bremse", "property"), ("Teileart", "Motor", "property")])
        rows = sent[0]["options"]["payload"]
        self.assertEqual([r["name"] for r in rows], ["Motor"])
        self.assertEqual(rows[0]["groupId"], "g-legacy")

    def test_ensure_groups_skips_existing(self):
        with patch.object(self.sw, "_with_client", lambda fn: self.fail("no write expected")), \
             patch("ecommerce_integrations.product_sync.engine.canonical._load_property_catalog", return_value={}):
            out = self.sw.ShopwareProductAdapter.ensure_property_entities_bulk(None, [("Teileart", "property")])
        self.assertEqual(out, {"property:Teileart": "g-legacy"})
