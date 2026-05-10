"""Resolver tests for the ``dry_run`` preview helper."""

import frappe
from frappe.tests import IntegrationTestCase

from ecommerce_integrations.smart_collections.engine.resolver import dry_run, resolve
from ecommerce_integrations.smart_collections.tests.test_helpers import (
    cleanup_test_data,
    make_item,
    make_item_group,
)


class TestResolverDryRun(IntegrationTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cleanup_test_data()
        cls.grp = make_item_group("TGroup-DR-Root", is_group=0)
        for i in range(60):
            make_item(
                f"TItem-DR-{i:03d}",
                item_group=cls.grp,
                properties={"System": "Stecksystem" if i < 40 else "Schraubsystem"},
            )

    @classmethod
    def tearDownClass(cls):
        cleanup_test_data()
        super().tearDownClass()

    def _coll(self, rules):
        return frappe.get_doc(
            {
                "doctype": "Ecommerce Smart Collection",
                "title": "Resolver DR Test",
                "slug": f"resolver-dr-test-{frappe.generate_hash(length=6)}",
                "rule_combinator": "AND",
                "rules": rules,
                "targets": [],
            }
        )

    def test_dry_run_returns_count_and_sample(self):
        coll = self._coll(
            [
                {"rule_type": "Item Group", "operator": "equals", "value": self.grp},
                {
                    "rule_type": "Ecommerce Property",
                    "field_key": "System",
                    "operator": "in",
                    "value": "Stecksystem",
                },
            ]
        )
        result = dry_run(coll)
        self.assertEqual(result["count"], 40)
        self.assertEqual(len(result["sample_items"]), 40)
        self.assertEqual(len(result["breakdown_by_rule"]), 2)

    def test_dry_run_caps_sample_at_50(self):
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
        result = dry_run(coll)
        self.assertGreaterEqual(result["count"], 40)
        self.assertLessEqual(len(result["sample_items"]), 50)

    def test_dry_run_breakdown_per_rule(self):
        coll = self._coll(
            [
                {"rule_type": "Item Group", "operator": "equals", "value": self.grp},
                {
                    "rule_type": "Ecommerce Property",
                    "field_key": "System",
                    "operator": "in",
                    "value": "Stecksystem",
                },
            ]
        )
        result = dry_run(coll)
        per_rule = {r["rule_index"]: r for r in result["breakdown_by_rule"]}
        self.assertEqual(per_rule[0]["matching_count"], 60)
        self.assertEqual(per_rule[1]["matching_count"], 40)

    def test_dry_run_does_not_persist_stats(self):
        coll = self._coll(
            [{"rule_type": "Item Group", "operator": "equals", "value": self.grp}]
        )
        coll.insert(ignore_permissions=True)
        try:
            before = (coll.last_resolved_count, coll.last_resolved_at)
            dry_run(coll)
            coll.reload()
            after = (coll.last_resolved_count, coll.last_resolved_at)
            self.assertEqual(before, after, "dry_run must not write the cache")
        finally:
            frappe.delete_doc(
                "Ecommerce Smart Collection",
                coll.name,
                force=True,
                ignore_permissions=True,
            )

    def test_resolve_persist_stats_false_does_not_persist(self):
        coll = self._coll(
            [{"rule_type": "Item Group", "operator": "equals", "value": self.grp}]
        )
        coll.insert(ignore_permissions=True)
        try:
            before = (coll.last_resolved_count, coll.last_resolved_at)
            resolve(coll, persist_stats=False)
            coll.reload()
            after = (coll.last_resolved_count, coll.last_resolved_at)
            self.assertEqual(before, after)
        finally:
            frappe.delete_doc(
                "Ecommerce Smart Collection",
                coll.name,
                force=True,
                ignore_permissions=True,
            )
