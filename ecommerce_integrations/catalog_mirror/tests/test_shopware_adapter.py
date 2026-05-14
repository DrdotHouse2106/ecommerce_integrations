"""Shopware adapter unit tests — mock HTTP, real DB.

Exercises the internal ``_*_impl`` methods directly so the
``temp_shopware_session`` decorator's test-mode short-circuit isn't
relevant — the adapter is instantiated bare and a MagicMock client is
passed in. This is exactly the pattern the Smart Collections adapter
tests use; the goal is to verify request payloads / pagination / 404
handling without standing up an OAuth client.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from ecommerce_integrations.catalog_mirror.engine.adapters.base import (
    AdapterError,
)
from ecommerce_integrations.catalog_mirror.engine.adapters.shopware import (
    ShopwareCatalogAdapter,
)


class TestShopwareCatalogAdapter(unittest.TestCase):
    def setUp(self):
        self.adapter = ShopwareCatalogAdapter()
        self.client = MagicMock()

    def test_upsert_create_returns_new_id(self):
        self.client.request_post.return_value = {"data": {"id": "new-uuid"}}
        ext_id = self.adapter._upsert_impl(
            self.client,
            external_id=None,
            parent_external_id="parent-uuid",
            name="Branch A",
            description=None,
            active=True,
            target_sales_channel=None,
        )
        self.assertEqual(ext_id, "new-uuid")
        # First call creates; check the payload shape.
        call = self.client.request_post.call_args_list[0]
        self.assertEqual(call.args[0], "category")
        payload = call.kwargs["payload"]
        self.assertEqual(payload["name"], "Branch A")
        self.assertEqual(payload["parentId"], "parent-uuid")
        self.assertTrue(payload["active"])

    def test_upsert_update_patches_existing(self):
        ext_id = self.adapter._upsert_impl(
            self.client,
            external_id="known-uuid",
            parent_external_id="parent-uuid",
            name="Renamed",
            description="desc",
            active=False,
            target_sales_channel=None,
        )
        self.assertEqual(ext_id, "known-uuid")
        self.client.request_patch.assert_called_once()
        patch_call = self.client.request_patch.call_args
        self.assertEqual(patch_call.args[0], "category/known-uuid")
        payload = patch_call.kwargs["payload"]
        self.assertEqual(payload["name"], "Renamed")
        self.assertEqual(payload["description"], "desc")
        self.assertFalse(payload["active"])
        # No create was triggered.
        self.client.request_post.assert_not_called()

    def test_upsert_update_falls_back_to_create_on_404(self):
        self.client.request_patch.side_effect = Exception(
            "Shopware 404 Not Found",
        )
        self.client.request_post.return_value = {"data": {"id": "fresh-uuid"}}
        ext_id = self.adapter._upsert_impl(
            self.client,
            external_id="stale-uuid",
            parent_external_id=None,
            name="Branch",
            description=None,
            active=True,
            target_sales_channel=None,
        )
        self.assertEqual(ext_id, "fresh-uuid")
        self.client.request_post.assert_called_once()

    def test_move_patches_parent_id(self):
        self.adapter._move_impl(self.client, "node-uuid", "new-parent-uuid")
        self.client.request_patch.assert_called_once()
        call = self.client.request_patch.call_args
        self.assertEqual(call.args[0], "category/node-uuid")
        self.assertEqual(
            call.kwargs["payload"], {"parentId": "new-parent-uuid"},
        )

    def test_delete_swallows_404(self):
        self.client.request_delete.side_effect = Exception(
            "Shopware 404 Not Found",
        )
        # Should not raise.
        self.adapter._delete_impl(self.client, "missing-uuid")

    def test_delete_propagates_other_errors(self):
        self.client.request_delete.side_effect = Exception("500 boom")
        with self.assertRaises(AdapterError):
            self.adapter._delete_impl(self.client, "uuid")

    def test_fetch_tree_paginates_children(self):
        # First call: fetch the root category.
        # Subsequent calls: BFS via /search/category, paginated.
        def request_get(path):
            if path == "category/live-root":
                return {
                    "data": {
                        "id": "live-root",
                        "attributes": {
                            "name": "Root",
                            "parentId": None,
                            "active": True,
                        },
                    },
                }
            return {}

        # /search/category responses keyed by parentId filter.
        def request_post(path, payload=None):
            if path != "search/category":
                return {}
            parent_filter = next(
                f for f in payload["filter"]
                if f["field"] == "parentId"
            )
            parent_id = parent_filter["value"]
            if parent_id == "live-root":
                return {
                    "data": [
                        {
                            "id": "child-a",
                            "attributes": {
                                "name": "A",
                                "parentId": "live-root",
                                "active": True,
                            },
                        },
                        {
                            "id": "child-b",
                            "attributes": {
                                "name": "B",
                                "parentId": "live-root",
                                "active": True,
                            },
                        },
                    ],
                }
            if parent_id == "child-a":
                return {
                    "data": [
                        {
                            "id": "child-a1",
                            "attributes": {
                                "name": "A1",
                                "parentId": "child-a",
                                "active": True,
                            },
                        },
                    ],
                }
            return {"data": []}

        self.client.request_get.side_effect = request_get
        self.client.request_post.side_effect = request_post

        root = self.adapter._fetch_tree_impl(self.client, "live-root")
        self.assertIsNotNone(root)
        self.assertEqual(root.external_id, "live-root")
        child_ids = {c.external_id for c in root.children}
        self.assertEqual(child_ids, {"child-a", "child-b"})
        # Find A's child.
        child_a = next(c for c in root.children if c.external_id == "child-a")
        self.assertEqual(
            [c.external_id for c in child_a.children], ["child-a1"],
        )

    def test_fetch_tree_returns_none_when_root_missing(self):
        self.client.request_get.side_effect = Exception("Shopware 404 Not Found")
        result = self.adapter._fetch_tree_impl(self.client, "missing-root")
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
