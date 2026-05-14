"""Differ tests: pure-Python, no DB, no HTTP.

Builds synthetic ``ErpTreeNode`` / ``LiveCategoryNode`` graphs and
asserts the diff buckets. The differ does call ``frappe.render_template``
once per node — when the template is a plain pass-through the fast
path is taken and the Frappe Jinja sandbox isn't touched, so this test
runs without a fully booted site context.
"""

from __future__ import annotations

import unittest

from ecommerce_integrations.catalog_mirror.differ import compute_tree_diff
from ecommerce_integrations.catalog_mirror.engine.adapters.base import (
    LiveCategoryNode,
)
from ecommerce_integrations.catalog_mirror.walker import ErpTreeNode


def _erp(name: str, item_group_name: str | None = None, **kw) -> ErpTreeNode:
    return ErpTreeNode(
        name=name,
        item_group_name=item_group_name or name,
        parent_item_group=kw.get("parent_item_group"),
        is_group=kw.get("is_group", 1),
        has_items=kw.get("has_items", False),
    )


def _live(external_id: str, name: str, **kw) -> LiveCategoryNode:
    return LiveCategoryNode(
        external_id=external_id,
        name=name,
        parent_external_id=kw.get("parent_external_id"),
        description=kw.get("description"),
        active=kw.get("active", True),
        product_count=kw.get("product_count", 0),
    )


class TestDiffer(unittest.TestCase):
    """Pure-Python differ checks — no Frappe DB required."""

    def setUp(self):
        # ERP tree:
        #   Root
        #   ├── A
        #   │   └── A1
        #   └── B
        self.root = _erp("Root", "Root Name")
        self.a = _erp("A", "Branch A", parent_item_group="Root")
        self.a1 = _erp("A1", "Leaf A1", parent_item_group="A", is_group=0, has_items=True)
        self.b = _erp("B", "Branch B", parent_item_group="Root")
        self.a.children = [self.a1]
        self.root.children = [self.a, self.b]

        # Live tree (initial state):
        #   live-root
        #   ├── live-a    (name="Branch A")
        #   │   └── live-a1 (name="Leaf A1")
        #   └── live-b    (name="Branch B")
        self.live_root = _live("live-root", "Root Name")
        self.live_a = _live("live-a", "Branch A", parent_external_id="live-root")
        self.live_a1 = _live("live-a1", "Leaf A1", parent_external_id="live-a")
        self.live_b = _live("live-b", "Branch B", parent_external_id="live-root")
        self.live_a.children = [self.live_a1]
        self.live_root.children = [self.live_a, self.live_b]

        self.mapping = {
            "Root": "live-root",
            "A": "live-a",
            "A1": "live-a1",
            "B": "live-b",
        }

    _SENTINEL = object()

    def _diff(self, mapping=None, live=_SENTINEL, **kw):
        return compute_tree_diff(
            erp_tree=self.root,
            live_tree=self.live_root if live is self._SENTINEL else live,
            mapping=mapping if mapping is not None else self.mapping,
            name_template=kw.get(
                "name_template", "{{ item_group.item_group_name }}",
            ),
            description_template=kw.get("description_template", ""),
            orphan_policy=kw.get("orphan_policy", "keep"),
        )

    def test_all_noops_when_in_sync(self):
        diff = self._diff()
        self.assertEqual(diff.creates, [])
        self.assertEqual(diff.updates, [])
        self.assertEqual(diff.moves, [])
        # All four nodes show up as noops (root + A + A1 + B).
        self.assertEqual(len(diff.noops), 4)
        self.assertEqual(diff.orphans, [])
        self.assertEqual(diff.mapping_drift, [])

    def test_create_for_new_ig(self):
        # Add a new IG with no mapping entry.
        c = _erp("C", "Branch C", parent_item_group="Root")
        self.root.children.append(c)
        diff = self._diff()
        creates = [p.item_group for p in diff.creates]
        self.assertIn("C", creates)
        # Its proposed parent is the live root id (resolved via mapping).
        c_plan = next(p for p in diff.creates if p.item_group == "C")
        self.assertEqual(c_plan.proposed_parent_external_id, "live-root")

    def test_update_when_name_changes(self):
        # Rename live-a so the diff sees a name drift.
        self.live_a.name = "Branch A (old)"
        diff = self._diff()
        updates = [p.item_group for p in diff.updates]
        self.assertEqual(updates, ["A"])
        # The other nodes are still noops.
        noop_names = [p.item_group for p in diff.noops]
        self.assertIn("Root", noop_names)
        self.assertIn("A1", noop_names)
        self.assertIn("B", noop_names)

    def test_move_when_parent_changes(self):
        # Re-parent A1 under B in ERP.
        self.a.children = []
        self.b.children = [self.a1]
        self.a1.parent_item_group = "B"
        diff = self._diff()
        move_names = [p.item_group for p in diff.moves]
        self.assertEqual(move_names, ["A1"])
        a1_plan = next(p for p in diff.moves if p.item_group == "A1")
        self.assertEqual(a1_plan.proposed_parent_external_id, "live-b")
        self.assertEqual(a1_plan.current_parent_external_id, "live-a")

    def test_orphans_under_root(self):
        # Add a live category that doesn't map to any IG.
        ghost = _live("live-ghost", "Ghost", parent_external_id="live-root")
        self.live_root.children.append(ghost)
        diff = self._diff()
        orphan_ids = [o.external_id for o in diff.orphans]
        self.assertEqual(orphan_ids, ["live-ghost"])

    def test_mapping_drift_when_recorded_id_missing(self):
        # Mapping points A at a live id that's not in the tree.
        bad_map = dict(self.mapping)
        bad_map["A"] = "live-deleted"
        diff = self._diff(mapping=bad_map)
        drift_names = [d.item_group for d in diff.mapping_drift]
        self.assertEqual(drift_names, ["A"])
        # A also falls into creates because its live match is gone.
        create_names = [p.item_group for p in diff.creates]
        self.assertIn("A", create_names)

    def test_unresolved_root_when_live_tree_missing(self):
        diff = self._diff(live=None)
        self.assertTrue(diff.unresolved_external_root)
        # All nodes go into creates.
        create_names = {p.item_group for p in diff.creates}
        self.assertEqual(create_names, {"Root", "A", "A1", "B"})


if __name__ == "__main__":
    unittest.main()
