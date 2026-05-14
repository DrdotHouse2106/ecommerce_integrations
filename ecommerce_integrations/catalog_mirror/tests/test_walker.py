"""Walker tests: real Item Group tree, real DB, no HTTP.

Builds a 3-level synthetic IG tree via ``frappe.get_doc("Item
Group").insert()`` and asserts the walker's output shape, leaf
pruning, and ``catalog_mirror_skip`` exclusion. Generic placeholder
identifiers only (CLAUDE.md rule).
"""

from __future__ import annotations

import frappe
from frappe.tests import IntegrationTestCase

from ecommerce_integrations.catalog_mirror.walker import walk_erpnext_tree


def _root_item_group() -> str:
    if frappe.db.exists("Item Group", "All Item Groups"):
        return "All Item Groups"
    name = frappe.db.get_value(
        "Item Group", {"is_group": 1}, "name", order_by="lft asc",
    )
    if not name:
        raise RuntimeError("No root Item Group on this site")
    return name


def _default_uom() -> str:
    for candidate in ("Nos", "Stk", "Einheit"):
        if frappe.db.exists("UOM", candidate):
            return candidate
    name = frappe.db.get_value("UOM", {}, "name")
    if not name:
        raise RuntimeError("No UOM on this site")
    return name


def _make_group(name: str, parent: str, is_group: int = 1) -> str:
    if frappe.db.exists("Item Group", name):
        return name
    frappe.get_doc({
        "doctype": "Item Group",
        "item_group_name": name,
        "parent_item_group": parent,
        "is_group": is_group,
    }).insert(ignore_permissions=True)
    return name


def _make_item(code: str, group: str) -> str:
    if frappe.db.exists("Item", code):
        return code
    frappe.get_doc({
        "doctype": "Item",
        "item_code": code,
        "item_name": code,
        "item_group": group,
        "stock_uom": _default_uom(),
        "is_sales_item": 1,
    }).insert(ignore_permissions=True)
    return code


class TestWalker(IntegrationTestCase):
    PREFIX = "TGroup-CMW"
    ITEM_PREFIX = "TItem-CMW"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._wipe()
        root_parent = _root_item_group()
        cls.root = _make_group(f"{cls.PREFIX}-Root", root_parent, is_group=1)
        cls.branch_a = _make_group(f"{cls.PREFIX}-A", cls.root, is_group=1)
        cls.branch_b = _make_group(f"{cls.PREFIX}-B", cls.root, is_group=1)
        cls.leaf_a1 = _make_group(f"{cls.PREFIX}-A1", cls.branch_a, is_group=0)
        cls.leaf_a2 = _make_group(f"{cls.PREFIX}-A2", cls.branch_a, is_group=0)
        cls.leaf_b1 = _make_group(f"{cls.PREFIX}-B1", cls.branch_b, is_group=0)
        # Empty leaf — should be pruned by default, included when
        # include_inactive_leaves=True.
        cls.leaf_empty = _make_group(
            f"{cls.PREFIX}-Empty", cls.branch_b, is_group=0,
        )
        _make_item(f"{cls.ITEM_PREFIX}-001", cls.leaf_a1)
        _make_item(f"{cls.ITEM_PREFIX}-002", cls.leaf_a2)
        _make_item(f"{cls.ITEM_PREFIX}-003", cls.leaf_b1)

    @classmethod
    def tearDownClass(cls):
        cls._wipe()
        super().tearDownClass()

    @classmethod
    def _wipe(cls):
        for code in frappe.get_all(
            "Item",
            filters={"item_code": ("like", f"{cls.ITEM_PREFIX}%")},
            pluck="name",
        ):
            try:
                frappe.delete_doc("Item", code, force=True, ignore_permissions=True)
            except Exception:
                pass
        for name in frappe.get_all(
            "Item Group",
            filters={"name": ("like", f"{cls.PREFIX}%")},
            pluck="name",
            order_by="lft desc",
        ):
            try:
                frappe.delete_doc(
                    "Item Group", name, force=True, ignore_permissions=True,
                )
            except Exception:
                pass

    def test_walker_shape(self):
        tree = walk_erpnext_tree(self.root)
        self.assertEqual(tree.name, self.root)
        names = {n.name for n in tree.iter_descendants()}
        self.assertIn(self.branch_a, names)
        self.assertIn(self.branch_b, names)
        self.assertIn(self.leaf_a1, names)
        # Empty leaf pruned by default.
        self.assertNotIn(self.leaf_empty, names)

    def test_walker_includes_empty_leaves_when_opted_in(self):
        tree = walk_erpnext_tree(self.root, include_inactive_leaves=True)
        names = {n.name for n in tree.iter_descendants()}
        self.assertIn(self.leaf_empty, names)

    def test_walker_respects_catalog_mirror_skip(self):
        # Set the skip flag on branch_b and assert it (and its
        # descendants) are pruned. The custom field is installed by the
        # setup_catalog_mirror patch — when it's not present on this
        # site the column won't exist and the test skips itself.
        if not frappe.db.has_column("Item Group", "catalog_mirror_skip"):
            self.skipTest("catalog_mirror_skip custom field not installed")
        frappe.db.set_value(
            "Item Group", self.branch_b, "catalog_mirror_skip", 1,
            update_modified=False,
        )
        try:
            tree = walk_erpnext_tree(self.root)
            names = {n.name for n in tree.iter_descendants()}
            self.assertNotIn(self.branch_b, names)
            self.assertNotIn(self.leaf_b1, names)
            # The other branch is untouched.
            self.assertIn(self.branch_a, names)
        finally:
            frappe.db.set_value(
                "Item Group", self.branch_b, "catalog_mirror_skip", 0,
                update_modified=False,
            )
