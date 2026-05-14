"""End-to-end Catalog Mirror foundation test with an in-memory adapter.

Builds a real Mirror doc + Item Group tree, swaps a stub adapter into
the registry, and exercises both ``apply_mirror(dry_run=True)`` and
``apply_mirror(dry_run=False)``. Asserts:

- The preview's bucket counts match the synthetic tree shape.
- The live run creates one live node per IG and persists
  ``shopware_category_id`` back onto each Item Group.
- A second live run is a noop (idempotency).
"""

from __future__ import annotations

import frappe
from frappe.tests import IntegrationTestCase

from ecommerce_integrations.catalog_mirror.engine.adapters.base import (
    CatalogAdapter,
    LiveCategoryNode,
    NodeMatch,
    _REGISTRY,
)
from ecommerce_integrations.catalog_mirror.engine.preview import (
    MirrorPreviewPlan,
    MirrorRunResult,
)
from ecommerce_integrations.catalog_mirror.tasks import apply_mirror


def _root_item_group() -> str:
    if frappe.db.exists("Item Group", "All Item Groups"):
        return "All Item Groups"
    return frappe.db.get_value(
        "Item Group", {"is_group": 1}, "name", order_by="lft asc",
    )


def _default_uom() -> str:
    for candidate in ("Nos", "Stk", "Einheit"):
        if frappe.db.exists("UOM", candidate):
            return candidate
    return frappe.db.get_value("UOM", {}, "name")


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


class _InMemoryAdapter(CatalogAdapter):
    """Tracks state in a class-level dict so the test can inspect it.

    Each instance reads/writes the same shared state via the class-level
    ``_NODES`` table; the orchestrator may construct a fresh adapter
    per call which would otherwise drop the in-memory tree on the floor.
    """

    backend = "Shopware"
    _NODES: dict[str, dict] = {}
    _next_id: int = 0

    @classmethod
    def reset(cls, root_external_id: str = "live-root") -> None:
        cls._NODES = {
            root_external_id: {
                "id": root_external_id,
                "parent": None,
                "name": "Live Root",
                "description": None,
                "active": True,
            },
        }
        cls._next_id = 0

    @classmethod
    def _mint(cls) -> str:
        cls._next_id += 1
        return f"live-{cls._next_id:03d}"

    def fetch_tree(self, root_external_id):
        if root_external_id not in self._NODES:
            return None
        return self._build_subtree(root_external_id)

    def _build_subtree(self, ext_id: str) -> LiveCategoryNode:
        row = self._NODES[ext_id]
        node = LiveCategoryNode(
            external_id=row["id"],
            name=row["name"],
            parent_external_id=row["parent"],
            description=row["description"],
            active=row["active"],
        )
        for child_id, child in self._NODES.items():
            if child["parent"] == ext_id:
                node.children.append(self._build_subtree(child_id))
        return node

    def find_matching_nodes(self, name, parent_external_id):
        results = []
        for row in self._NODES.values():
            if row["name"] != name:
                continue
            if parent_external_id and row["parent"] != parent_external_id:
                continue
            results.append(
                NodeMatch(
                    external_id=row["id"], name=row["name"], path=row["name"],
                ),
            )
        return results

    def upsert_node(
        self,
        *,
        external_id,
        parent_external_id,
        name,
        description,
        active,
        target_sales_channel,
    ):
        if external_id and external_id in self._NODES:
            row = self._NODES[external_id]
            row["name"] = name
            row["description"] = description
            row["active"] = active
            if parent_external_id is not None:
                row["parent"] = parent_external_id
            return external_id
        new_id = self._mint()
        self._NODES[new_id] = {
            "id": new_id,
            "parent": parent_external_id,
            "name": name,
            "description": description,
            "active": active,
        }
        return new_id

    def move_node(self, external_id, new_parent_external_id):
        if external_id not in self._NODES:
            return
        self._NODES[external_id]["parent"] = new_parent_external_id

    def delete_node(self, external_id):
        self._NODES.pop(external_id, None)


class TestFoundationIntegration(IntegrationTestCase):
    PREFIX = "TGroup-CMI"
    ITEM_PREFIX = "TItem-CMI"
    MIRROR_TITLE = "test-catalog-mirror-foundation"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._wipe()
        root_parent = _root_item_group()
        cls.root_ig = _make_group(f"{cls.PREFIX}-Root", root_parent, is_group=1)
        cls.branch = _make_group(f"{cls.PREFIX}-Branch", cls.root_ig, is_group=1)
        cls.leaf = _make_group(
            f"{cls.PREFIX}-Leaf", cls.branch, is_group=0,
        )
        _make_item(f"{cls.ITEM_PREFIX}-001", cls.leaf)

        cls._orig_registry = dict(_REGISTRY)
        _REGISTRY["Shopware"] = _InMemoryAdapter

        # Reset the in-memory backend before we create the mirror.
        _InMemoryAdapter.reset(root_external_id="live-root")

        # Create the Mirror with the live root already pinned so the
        # orchestrator doesn't try to resolve the sales-channel root.
        mirror = frappe.get_doc({
            "doctype": "Ecommerce Catalog Mirror",
            "title": cls.MIRROR_TITLE,
            "backend": "Shopware",
            "is_active": 1,
            "root_item_group": cls.root_ig,
            "external_root_id": "live-root",
            "name_template": "{{ item_group.item_group_name }}",
            "orphan_policy": "keep",
        }).insert(ignore_permissions=True)
        cls.mirror_name = mirror.name

    @classmethod
    def tearDownClass(cls):
        _REGISTRY.clear()
        _REGISTRY.update(cls._orig_registry)
        cls._wipe()
        super().tearDownClass()

    @classmethod
    def _wipe(cls):
        if frappe.db.exists("Ecommerce Catalog Mirror", cls.MIRROR_TITLE):
            try:
                frappe.delete_doc(
                    "Ecommerce Catalog Mirror", cls.MIRROR_TITLE,
                    force=True, ignore_permissions=True,
                )
            except Exception:
                pass
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

    def test_dry_run_produces_preview_plan(self):
        plan = apply_mirror(self.mirror_name, dry_run=True)
        self.assertIsInstance(plan, MirrorPreviewPlan)
        self.assertEqual(plan.backend, "Shopware")
        # The root IG is already represented by ``live-root`` (the
        # orchestrator threads it into the diff mapping), so the
        # non-root IGs are the creates.
        create_groups = {p.item_group for p in plan.creates}
        self.assertIn(self.branch, create_groups)
        self.assertIn(self.leaf, create_groups)

    def test_live_run_persists_mappings(self):
        # Reset adapter state so we count from zero.
        _InMemoryAdapter.reset(root_external_id="live-root")
        # Clear any IG mapping from a previous test run.
        for ig in (self.branch, self.leaf):
            frappe.db.set_value(
                "Item Group", ig, "shopware_category_id", "",
                update_modified=False,
            )

        result = apply_mirror(self.mirror_name, dry_run=False)
        self.assertIsInstance(result, MirrorRunResult)
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.created, 2)  # branch + leaf
        # Mappings persisted.
        for ig in (self.branch, self.leaf):
            value = frappe.db.get_value(
                "Item Group", ig, "shopware_category_id",
            )
            self.assertTrue(
                value, f"expected shopware_category_id on {ig}, got {value!r}",
            )

    def test_second_live_run_is_idempotent(self):
        # Re-run apply against the in-memory backend that the previous
        # test left populated; the second run should report zero
        # creates / updates / moves.
        result = apply_mirror(self.mirror_name, dry_run=False)
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.created, 0)
        self.assertEqual(result.updated, 0)
        self.assertEqual(result.moved, 0)
