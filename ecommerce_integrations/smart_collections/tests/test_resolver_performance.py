"""Opt-in performance smoketest with 10k synthetic items.

Skipped by default — set ``RUN_PERF=1`` to enable. Intended for a clean
test bench, *not* a production site (creates 10k Items via the test
helpers, then cleans up). See spec §4.5 for the runtime budget.
"""

import os
import time
import unittest

import frappe
from frappe.tests import IntegrationTestCase

from ecommerce_integrations.smart_collections.engine.resolver import resolve
from ecommerce_integrations.smart_collections.tests.test_helpers import (
    cleanup_test_data,
    make_item,
    make_item_group,
)


@unittest.skipUnless(os.environ.get("RUN_PERF"), "set RUN_PERF=1 to enable")
class TestResolverPerformance(IntegrationTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cleanup_test_data()
        cls.grp = make_item_group("TGroup-PF-Root", is_group=0)
        for i in range(10000):
            make_item(
                f"TItem-PF-{i:05d}",
                item_group=cls.grp,
                properties={
                    "System": "Stecksystem" if i % 2 == 0 else "Schraubsystem"
                },
            )

    @classmethod
    def tearDownClass(cls):
        cleanup_test_data()
        super().tearDownClass()

    def test_resolve_10k_under_2_seconds(self):
        coll = frappe.get_doc(
            {
                "doctype": "Ecommerce Smart Collection",
                "title": "Resolver PF Test",
                "slug": "resolver-pf-test",
                "rule_combinator": "AND",
                "rules": [
                    {
                        "rule_type": "Item Group",
                        "operator": "equals",
                        "value": self.grp,
                    },
                    {
                        "rule_type": "Ecommerce Property",
                        "field_key": "System",
                        "operator": "in",
                        "value": "Stecksystem",
                    },
                ],
                "targets": [],
            }
        )
        start = time.perf_counter()
        result = resolve(coll)
        elapsed = time.perf_counter() - start
        self.assertEqual(len(result), 5000)
        self.assertLess(elapsed, 2.0, f"Resolver took {elapsed:.2f}s, expected <2s")
        print(f"\n[perf] Resolved 10k items in {elapsed:.3f}s")
