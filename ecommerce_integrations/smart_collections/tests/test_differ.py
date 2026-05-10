"""Differ tests: snapshot roundtrip, first-run, partial change, overwrite."""

import frappe
from frappe.tests import IntegrationTestCase

from ecommerce_integrations.smart_collections.engine.differ import (
    compute_diff,
    load_snapshot,
    save_snapshot,
)


class TestDiffer(IntegrationTestCase):
    SLUG = "test-differ-coll"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._wipe_state()
        cls.coll_name = frappe.get_doc(
            {
                "doctype": "Ecommerce Smart Collection",
                "title": "Test Differ Coll",
                "slug": cls.SLUG,
                "rule_combinator": "AND",
                "rules": [],
                "targets": [
                    {"backend": "Medusa", "sales_channel": "test-ch", "enabled": 1}
                ],
            }
        ).insert(ignore_permissions=True).name

    @classmethod
    def tearDownClass(cls):
        cls._wipe_state()
        super().tearDownClass()

    @classmethod
    def _wipe_state(cls):
        for n in frappe.get_all(
            "Ecommerce Smart Collection Snapshot",
            filters={"collection": ("like", f"{cls.SLUG}%")},
            pluck="name",
        ):
            frappe.delete_doc(
                "Ecommerce Smart Collection Snapshot",
                n,
                force=True,
                ignore_permissions=True,
            )
        for n in frappe.get_all(
            "Ecommerce Smart Collection",
            filters={"slug": ("like", f"{cls.SLUG}%")},
            pluck="name",
        ):
            frappe.delete_doc(
                "Ecommerce Smart Collection",
                n,
                force=True,
                ignore_permissions=True,
            )

    def setUp(self):
        # Reset snapshot state between tests so each one starts clean.
        snap_name = f"{self.coll_name}-1"
        if frappe.db.exists("Ecommerce Smart Collection Snapshot", snap_name):
            frappe.delete_doc(
                "Ecommerce Smart Collection Snapshot",
                snap_name,
                force=True,
                ignore_permissions=True,
            )

    def test_diff_first_run_all_added(self):
        diff = compute_diff(self.coll_name, target_idx=1, new_items={"A", "B", "C"})
        self.assertEqual(diff["to_add"], {"A", "B", "C"})
        self.assertEqual(diff["to_remove"], set())
        self.assertEqual(diff["to_keep"], set())

    def test_diff_after_save_no_change(self):
        save_snapshot(self.coll_name, 1, {"A", "B"})
        diff = compute_diff(self.coll_name, 1, {"A", "B"})
        self.assertEqual(diff["to_add"], set())
        self.assertEqual(diff["to_remove"], set())
        self.assertEqual(diff["to_keep"], {"A", "B"})

    def test_diff_partial_change(self):
        save_snapshot(self.coll_name, 1, {"A", "B", "C"})
        diff = compute_diff(self.coll_name, 1, {"B", "C", "D"})
        self.assertEqual(diff["to_add"], {"D"})
        self.assertEqual(diff["to_remove"], {"A"})
        self.assertEqual(diff["to_keep"], {"B", "C"})

    def test_save_snapshot_overwrites_in_place(self):
        save_snapshot(self.coll_name, 1, {"X"})
        save_snapshot(self.coll_name, 1, {"Y", "Z"})
        self.assertEqual(load_snapshot(self.coll_name, 1), {"Y", "Z"})
        diff = compute_diff(self.coll_name, 1, {"Y", "Z"})
        self.assertEqual(diff["to_add"], set())
        self.assertEqual(diff["to_keep"], {"Y", "Z"})

    def test_load_snapshot_missing_returns_empty_set(self):
        self.assertEqual(load_snapshot(self.coll_name, 99), set())
