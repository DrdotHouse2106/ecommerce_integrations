"""Dry-run preview + adopt-match integration tests.

Covers the four contract points the form depends on:

- ``preview_collection`` returns ``action='create'`` when the target
  has no ``external_id`` yet,
- it also returns ``action='create'`` when ``external_id`` is set but
  the live backend returns 404 (stale id),
- ``link_only=1`` marks ``to_remove_skipped`` rather than silently
  hiding the count,
- ``adopt_match`` sets ``external_id`` correctly and refuses without
  write permission.

Adapter HTTP calls are stubbed via the registry so the tests run
without a live Shopware/Medusa backend; the DB is real per the
plugin's testing rule.
"""

from typing import ClassVar
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from ecommerce_integrations.smart_collections.engine.adapters.base import (
    _REGISTRY,
    CategoryAdapter,
    CategoryMatch,
    LiveCategoryState,
)
from ecommerce_integrations.smart_collections.engine.preview import (
    CollectionSyncPlan,
)
from ecommerce_integrations.smart_collections.tasks import sync_collection
from ecommerce_integrations.smart_collections.tests.test_helpers import (
    cleanup_test_data,
    make_item,
    make_item_group,
)


class _StubAdapter(CategoryAdapter):
    """Adapter test-double — every method is overridable from the test."""

    backend = "Medusa"
    _stub_state: LiveCategoryState = LiveCategoryState(exists=False)
    _stub_matches: ClassVar[list[CategoryMatch]] = []

    def upsert_category(self, collection, target) -> str:
        return "stub-ext"

    def link_items(self, target, item_codes):
        return []

    def unlink_items(self, target, item_codes):
        return None

    def delete_category(self, target):
        return None

    def fetch_state(self, target) -> LiveCategoryState:
        return type(self)._stub_state

    def find_matching_category(self, collection, target):
        return list(type(self)._stub_matches)


class TestPreview(IntegrationTestCase):
    SLUG_PREFIX = "test-preview-"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cleanup_test_data()
        cls._wipe_collections()
        cls.grp = make_item_group("TGroup-PV-Root", is_group=0)
        for i in range(5):
            make_item(f"TItem-PV-{i:03d}", item_group=cls.grp)

        cls._orig_registry = dict(_REGISTRY)
        _REGISTRY["Medusa"] = _StubAdapter
        _REGISTRY["Shopware"] = _StubAdapter

    @classmethod
    def tearDownClass(cls):
        _REGISTRY.clear()
        _REGISTRY.update(cls._orig_registry)
        cls._wipe_collections()
        cleanup_test_data()
        super().tearDownClass()

    @classmethod
    def _wipe_collections(cls):
        for n in frappe.get_all(
            "Ecommerce Smart Collection",
            filters={"slug": ("like", f"{cls.SLUG_PREFIX}%")},
            pluck="name",
        ):
            try:
                frappe.delete_doc(
                    "Ecommerce Smart Collection",
                    n,
                    force=True,
                    ignore_permissions=True,
                )
            except Exception:
                pass

    def _make_collection(self, *, link_only: int = 0, external_id: str = "") -> str:
        slug = (
            self.SLUG_PREFIX + frappe.generate_hash(length=6).lower()
        )
        doc = frappe.get_doc(
            {
                "doctype": "Ecommerce Smart Collection",
                "title": "Preview Test Coll",
                "slug": slug,
                "is_active": 1,
                "rule_combinator": "AND",
                "rules": [
                    {
                        "rule_type": "Item Group",
                        "operator": "equals",
                        "value": self.grp,
                    }
                ],
                "targets": [
                    {
                        "backend": "Medusa",
                        "sales_channel": "test-ch",
                        "enabled": 1,
                        "link_only": link_only,
                        "external_id": external_id,
                    }
                ],
            }
        ).insert(ignore_permissions=True)
        return doc.name

    def setUp(self):
        # Reset the stub between tests so leakage can't make one test
        # silently depend on another's setup.
        _StubAdapter._stub_state = LiveCategoryState(exists=False)
        _StubAdapter._stub_matches = []

    def test_preview_no_external_id_returns_create(self):
        name = self._make_collection()
        plan = sync_collection(name, dry_run=True)
        self.assertIsInstance(plan, CollectionSyncPlan)
        self.assertEqual(len(plan.targets), 1)
        t = plan.targets[0]
        self.assertEqual(t.action, "create")
        self.assertFalse(t.live_exists)
        # Empty backend → every resolved item is in to_add.
        self.assertEqual(len(t.to_add), 5)
        self.assertEqual(t.to_remove, [])

    def test_preview_stale_external_id_returns_create(self):
        # Stub: external_id set on the row but fetch_state returns
        # exists=False, which the adapter normalises 404s into.
        name = self._make_collection(external_id="stale-cat-id")
        _StubAdapter._stub_state = LiveCategoryState(exists=False)
        plan = sync_collection(name, dry_run=True)
        t = plan.targets[0]
        self.assertEqual(t.action, "create")
        self.assertFalse(t.live_exists)
        # A note must mark the stale id so the operator notices.
        self.assertTrue(any("stale" in n for n in t.notes), t.notes)

    def test_preview_link_only_marks_to_remove_skipped(self):
        # Pre-populate the snapshot so to_remove is non-empty: snap has
        # extra items not in the current resolver output.
        from ecommerce_integrations.smart_collections.engine.differ import (
            save_snapshot,
        )

        name = self._make_collection(link_only=1)
        coll = frappe.get_doc("Ecommerce Smart Collection", name)
        target_idx = coll.targets[0].idx
        # Save a snapshot with an extra item not in the resolved set.
        save_snapshot(
            name, target_idx,
            {f"TItem-PV-{i:03d}" for i in range(5)} | {"TItem-PV-EXTRA"},
        )
        plan = sync_collection(name, dry_run=True)
        t = plan.targets[0]
        self.assertGreaterEqual(len(t.to_remove), 1)
        self.assertTrue(t.to_remove_skipped)
        self.assertEqual(t.link_only, 1)
        # And there must be an operator-facing note about the skip.
        self.assertTrue(
            any("link_only" in n for n in t.notes),
            t.notes,
        )

    def test_preview_returns_match_suggestions(self):
        _StubAdapter._stub_matches = [
            CategoryMatch(
                external_id="cat-suggest-1",
                name="Preview Test Coll",
                path="root / Preview Test Coll",
                has_target_sales_channel=True,
                linked_product_count=7,
            )
        ]
        name = self._make_collection()
        plan = sync_collection(name, dry_run=True)
        t = plan.targets[0]
        self.assertEqual(len(t.match_suggestions), 1)
        self.assertEqual(t.match_suggestions[0]["external_id"], "cat-suggest-1")
        self.assertTrue(t.match_suggestions[0]["has_target_sales_channel"])

    def test_adopt_match_sets_external_id(self):
        from ecommerce_integrations.smart_collections.api import adopt_match

        name = self._make_collection()
        coll = frappe.get_doc("Ecommerce Smart Collection", name)
        target_name = coll.targets[0].name
        result = adopt_match(target_name, "adopted-ext-id", link_only=1)
        self.assertEqual(result["external_id"], "adopted-ext-id")
        self.assertEqual(result["link_only"], 1)
        target_row = frappe.get_doc(
            "Ecommerce Smart Collection Target", target_name,
        )
        self.assertEqual(target_row.external_id, "adopted-ext-id")
        self.assertEqual(int(target_row.link_only or 0), 1)
        self.assertEqual(target_row.sync_status, "pending")

    def test_adopt_match_refuses_without_permission(self):
        from ecommerce_integrations.smart_collections.api import adopt_match

        name = self._make_collection()
        coll = frappe.get_doc("Ecommerce Smart Collection", name)
        target_name = coll.targets[0].name

        # Patch has_permission to deny the parent write — adopt_match
        # routes through parent.check_permission("write").
        with patch.object(
            frappe.model.document.Document, "check_permission",
        ) as cp:
            cp.side_effect = frappe.PermissionError("denied")
            with self.assertRaises(frappe.PermissionError):
                adopt_match(target_name, "adopted-ext-id")

    def test_preview_dry_run_does_not_write_target(self):
        name = self._make_collection()
        coll_before = frappe.get_doc("Ecommerce Smart Collection", name)
        before_status = coll_before.targets[0].sync_status
        before_synced = coll_before.targets[0].last_synced_at

        sync_collection(name, dry_run=True)

        coll_after = frappe.get_doc("Ecommerce Smart Collection", name)
        self.assertEqual(coll_after.targets[0].sync_status, before_status)
        self.assertEqual(coll_after.targets[0].last_synced_at, before_synced)
