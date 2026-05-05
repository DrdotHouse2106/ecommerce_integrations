"""Resolver tests: combinator, OR-groups, standard filters, caching."""

import frappe
from frappe.tests import IntegrationTestCase

from ecommerce_integrations.smart_collections.engine.resolver import resolve
from ecommerce_integrations.smart_collections.tests.test_helpers import (
    cleanup_test_data,
    make_item,
    make_item_group,
)


class TestResolverCombinator(IntegrationTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cleanup_test_data()
        cls.grp = make_item_group("TGroup-CB-Root", is_group=0)
        cls.a = make_item(
            "TItem-CB-1",
            item_group=cls.grp,
            properties={"System": "Stecksystem", "Material": "Stahl"},
        )
        cls.b = make_item(
            "TItem-CB-2",
            item_group=cls.grp,
            properties={"System": "Schraubsystem", "Material": "Stahl"},
        )
        cls.c = make_item(
            "TItem-CB-3",
            item_group=cls.grp,
            properties={"System": "Stecksystem"},
        )

    @classmethod
    def tearDownClass(cls):
        cleanup_test_data()
        super().tearDownClass()

    def _coll(self, combinator, rules):
        return frappe.get_doc(
            {
                "doctype": "Ecommerce Smart Collection",
                "title": "Resolver CB Test",
                "slug": f"resolver-cb-test-{frappe.generate_hash(length=6)}",
                "rule_combinator": combinator,
                "rules": rules,
                "targets": [],
            }
        )

    def test_and_intersection(self):
        coll = self._coll(
            "AND",
            [
                {"rule_type": "Item Group", "operator": "equals", "value": self.grp},
                {
                    "rule_type": "Ecommerce Property",
                    "field_key": "System",
                    "operator": "in",
                    "value": "Stecksystem",
                },
                {
                    "rule_type": "Ecommerce Property",
                    "field_key": "Material",
                    "operator": "in",
                    "value": "Stahl",
                },
            ],
        )
        self.assertEqual(resolve(coll), {self.a})

    def test_or_union(self):
        coll = self._coll(
            "OR",
            [
                {
                    "rule_type": "Ecommerce Property",
                    "field_key": "System",
                    "operator": "in",
                    "value": "Schraubsystem",
                },
                {
                    "rule_type": "Ecommerce Property",
                    "field_key": "Material",
                    "operator": "is_empty",
                    "value": "",
                },
            ],
        )
        # OR pulls in any item matching either rule. Items with no
        # Material property match is_empty; that includes test items
        # outside this test's grp.
        result = resolve(coll)
        self.assertIn(self.b, result)
        self.assertIn(self.c, result)

    def test_or_group_within_and(self):
        # AND-combined groups: (System=Steck OR System=Schraub) AND Material=Stahl
        coll = self._coll(
            "AND",
            [
                {
                    "rule_type": "Ecommerce Property",
                    "field_key": "System",
                    "operator": "in",
                    "value": "Stecksystem",
                    "group_id": 1,
                },
                {
                    "rule_type": "Ecommerce Property",
                    "field_key": "System",
                    "operator": "in",
                    "value": "Schraubsystem",
                    "group_id": 1,
                },
                {
                    "rule_type": "Ecommerce Property",
                    "field_key": "Material",
                    "operator": "in",
                    "value": "Stahl",
                    "group_id": 2,
                },
            ],
        )
        self.assertEqual(resolve(coll), {self.a, self.b})

    def test_disabled_item_excluded(self):
        frappe.db.set_value("Item", self.a, "disabled", 1)
        try:
            coll = self._coll(
                "AND",
                [{"rule_type": "Item Group", "operator": "equals", "value": self.grp}],
            )
            result = resolve(coll)
            self.assertNotIn(self.a, result)
            self.assertIn(self.b, result)
        finally:
            frappe.db.set_value("Item", self.a, "disabled", 0)

    def test_unpublished_item_excluded(self):
        frappe.db.set_value("Item", self.b, "published_in_website", 0)
        try:
            coll = self._coll(
                "AND",
                [{"rule_type": "Item Group", "operator": "equals", "value": self.grp}],
            )
            self.assertNotIn(self.b, resolve(coll))
        finally:
            frappe.db.set_value("Item", self.b, "published_in_website", 1)

    def test_resolver_caches_count_when_persisted(self):
        coll = self._coll(
            "AND",
            [{"rule_type": "Item Group", "operator": "equals", "value": self.grp}],
        )
        coll.insert(ignore_permissions=True)
        try:
            items = resolve(coll)
            coll.reload()
            self.assertEqual(coll.last_resolved_count, len(items))
            self.assertIsNotNone(coll.last_resolved_at)
        finally:
            frappe.delete_doc(
                "Ecommerce Smart Collection",
                coll.name,
                force=True,
                ignore_permissions=True,
            )
