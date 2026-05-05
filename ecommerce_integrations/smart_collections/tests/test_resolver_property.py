"""Resolver tests for Ecommerce Property rules — strict NULL §4.4."""

import frappe
from frappe.tests import IntegrationTestCase

from ecommerce_integrations.smart_collections.engine.resolver import resolve
from ecommerce_integrations.smart_collections.tests.test_helpers import (
    cleanup_test_data,
    make_item,
)


class TestResolverProperty(IntegrationTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cleanup_test_data()
        cls.steck = make_item("TItem-PR-1", properties={"System": "Stecksystem"})
        cls.schraub = make_item("TItem-PR-2", properties={"System": "Schraubsystem"})
        cls.no_prop = make_item("TItem-PR-3")
        cls.steck_alu = make_item(
            "TItem-PR-4",
            properties={"System": "Stecksystem", "Material": "Aluminium"},
        )

    @classmethod
    def tearDownClass(cls):
        cleanup_test_data()
        super().tearDownClass()

    def _coll(self, rules, combinator="AND"):
        return frappe.get_doc(
            {
                "doctype": "Ecommerce Smart Collection",
                "title": "Resolver PR Test",
                "slug": f"resolver-pr-test-{frappe.generate_hash(length=6)}",
                "rule_combinator": combinator,
                "rules": rules,
                "targets": [],
            }
        )

    def test_in_strict_null(self):
        coll = self._coll(
            [
                {
                    "rule_type": "Ecommerce Property",
                    "field_key": "System",
                    "operator": "in",
                    "value": "Stecksystem",
                }
            ]
        )
        result = resolve(coll)
        self.assertEqual(result, {self.steck, self.steck_alu})
        self.assertNotIn(self.no_prop, result)

    def test_equals(self):
        coll = self._coll(
            [
                {
                    "rule_type": "Ecommerce Property",
                    "field_key": "System",
                    "operator": "equals",
                    "value": "Schraubsystem",
                }
            ]
        )
        self.assertEqual(resolve(coll), {self.schraub})

    def test_not_in_strict_null_excludes_unset(self):
        coll = self._coll(
            [
                {
                    "rule_type": "Ecommerce Property",
                    "field_key": "System",
                    "operator": "not_in",
                    "value": "Schraubsystem",
                }
            ]
        )
        result = resolve(coll)
        self.assertIn(self.steck, result)
        self.assertIn(self.steck_alu, result)
        self.assertNotIn(self.schraub, result)
        self.assertNotIn(
            self.no_prop,
            result,
            "Items without System property must NOT match not_in (strict NULL §4.4)",
        )

    def test_is_set_matches_only_with_property(self):
        coll = self._coll(
            [
                {
                    "rule_type": "Ecommerce Property",
                    "field_key": "System",
                    "operator": "is_set",
                    "value": "",
                }
            ]
        )
        self.assertEqual(resolve(coll), {self.steck, self.schraub, self.steck_alu})

    def test_is_empty_matches_only_without_property(self):
        coll = self._coll(
            [
                {
                    "rule_type": "Ecommerce Property",
                    "field_key": "System",
                    "operator": "is_empty",
                    "value": "",
                }
            ]
        )
        self.assertEqual(resolve(coll), {self.no_prop})

    def test_or_group_combines_not_in_with_is_empty(self):
        coll = self._coll(
            [
                {
                    "rule_type": "Ecommerce Property",
                    "field_key": "System",
                    "operator": "not_in",
                    "value": "Schraubsystem",
                    "group_id": 1,
                },
                {
                    "rule_type": "Ecommerce Property",
                    "field_key": "System",
                    "operator": "is_empty",
                    "value": "",
                    "group_id": 1,
                },
            ]
        )
        self.assertEqual(resolve(coll), {self.steck, self.steck_alu, self.no_prop})

    def test_contains(self):
        coll = self._coll(
            [
                {
                    "rule_type": "Ecommerce Property",
                    "field_key": "Material",
                    "operator": "contains",
                    "value": "lumin",
                }
            ]
        )
        self.assertEqual(resolve(coll), {self.steck_alu})

    def test_regex(self):
        coll = self._coll(
            [
                {
                    "rule_type": "Ecommerce Property",
                    "field_key": "System",
                    "operator": "regex",
                    "value": "^Steck",
                }
            ]
        )
        self.assertEqual(resolve(coll), {self.steck, self.steck_alu})
