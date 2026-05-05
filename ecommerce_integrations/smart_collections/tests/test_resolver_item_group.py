"""Resolver tests for Item Group rules (all five operators)."""

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils.nestedset import rebuild_tree

from ecommerce_integrations.smart_collections.engine.resolver import resolve
from ecommerce_integrations.smart_collections.tests.test_helpers import (
    cleanup_test_data,
    make_item,
    make_item_group,
    root_item_group,
)


class TestResolverItemGroup(IntegrationTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cleanup_test_data()
        # Hierarchy: TGroup-IG-Root (group) → TGroup-IG-Sub (leaf)
        cls.root_group = root_item_group()
        cls.root = make_item_group("TGroup-IG-Root", parent=cls.root_group, is_group=1)
        cls.sub = make_item_group("TGroup-IG-Sub", parent=cls.root, is_group=0)
        # Frappe builds lft/rgt on each save; force a clean rebuild for
        # the subtree we just inserted so descends_from queries are correct.
        rebuild_tree("Item Group")
        cls.item_in_sub = make_item("TItem-IG-1", item_group=cls.sub)
        cls.item_in_root = make_item("TItem-IG-2", item_group=cls.root)
        cls.item_outside = make_item("TItem-IG-3", item_group=cls.root_group)

    @classmethod
    def tearDownClass(cls):
        cleanup_test_data()
        super().tearDownClass()

    def _make_collection(self, rules):
        return frappe.get_doc(
            {
                "doctype": "Ecommerce Smart Collection",
                "title": "Resolver IG Test",
                "slug": f"resolver-ig-test-{frappe.generate_hash(length=6)}",
                "rule_combinator": "AND",
                "rules": rules,
                "targets": [],
            }
        )

    def test_descends_from_includes_subgroup_items(self):
        coll = self._make_collection(
            [{"rule_type": "Item Group", "operator": "descends_from", "value": self.root}]
        )
        result = resolve(coll)
        self.assertIn(self.item_in_sub, result)
        self.assertIn(self.item_in_root, result)
        self.assertNotIn(self.item_outside, result)

    def test_equals_only_direct_match(self):
        coll = self._make_collection(
            [{"rule_type": "Item Group", "operator": "equals", "value": self.sub}]
        )
        self.assertEqual(resolve(coll), {self.item_in_sub})

    def test_in_with_multiple_groups(self):
        coll = self._make_collection(
            [
                {
                    "rule_type": "Item Group",
                    "operator": "in",
                    "value": f"{self.sub},{self.root}",
                }
            ]
        )
        self.assertEqual(resolve(coll), {self.item_in_sub, self.item_in_root})

    def test_not_in_excludes_groups(self):
        coll = self._make_collection(
            [{"rule_type": "Item Group", "operator": "not_in", "value": self.sub}]
        )
        result = resolve(coll)
        self.assertNotIn(self.item_in_sub, result)
        self.assertIn(self.item_in_root, result)

    def test_negate_inverts_clause(self):
        coll = self._make_collection(
            [
                {
                    "rule_type": "Item Group",
                    "operator": "equals",
                    "value": self.sub,
                    "negate": 1,
                }
            ]
        )
        result = resolve(coll)
        self.assertNotIn(self.item_in_sub, result)
        self.assertIn(self.item_in_root, result)
        self.assertIn(self.item_outside, result)
