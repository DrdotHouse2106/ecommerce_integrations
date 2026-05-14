"""Medusa adapter unit tests — mock HTTP, real DB.

The adapter wraps ``optional_session()`` / ``medusa_request()`` from
``medusa.connection``. To exercise it without a live Medusa server we
patch both at the import path the adapter uses:

- ``optional_session`` is replaced with a no-op context manager that
  yields a sentinel pair ``(session=None, base_url="")``.
- ``medusa_request`` is replaced with a side-effect-driven mock that
  routes by ``(method, path)`` to canned responses.

This is the same pattern the Smart Collections adapter tests use.
Adapter behaviour (payload shape, pagination, 404 silence, handle-
collision recovery, dedup) is asserted against the recorded calls.
"""

from __future__ import annotations

import contextlib
import unittest
from unittest.mock import MagicMock, patch

import requests

from ecommerce_integrations.catalog_mirror.engine.adapters import (
    medusa as medusa_adapter,
)
from ecommerce_integrations.catalog_mirror.engine.adapters.base import (
    AdapterError,
)


@contextlib.contextmanager
def _noop_session():
    """Replacement for ``optional_session`` — yields a sentinel pair."""
    yield (None, "")


def _http_error(status: int, body: str = "") -> requests.exceptions.HTTPError:
    """Construct an HTTPError that the adapter's error inspectors recognise."""
    resp = MagicMock()
    resp.status_code = status
    resp.text = body
    err = requests.exceptions.HTTPError(f"{status} error: {body}")
    err.response = resp
    return err


class TestMedusaCatalogAdapter(unittest.TestCase):
    def setUp(self):
        self.adapter = medusa_adapter.MedusaCatalogAdapter()

        # Patch the two connection-layer entry points the adapter uses.
        # Each test sets its own ``side_effect`` on ``self.request_mock``.
        self.session_patcher = patch.object(
            medusa_adapter, "optional_session", _noop_session,
        )
        self.request_patcher = patch.object(
            medusa_adapter, "medusa_request",
        )
        self.session_patcher.start()
        self.request_mock = self.request_patcher.start()
        self.addCleanup(self.session_patcher.stop)
        self.addCleanup(self.request_patcher.stop)

    # ── fetch_tree ───────────────────────────────────────────────────

    def test_fetch_tree_paginates_children(self):
        """BFS walks all children; pagination stops when page < limit."""

        def fake_request(session, base_url, method, path, **kwargs):
            params = kwargs.get("params") or {}
            json_body = kwargs.get("json")
            del session, base_url, json_body  # unused

            if method == "GET" and path == "/admin/product-categories/live-root":
                return {
                    "product_category": {
                        "id": "live-root",
                        "name": "Root",
                        "handle": "root",
                        "is_active": True,
                        "parent_category_id": None,
                        "description": None,
                    },
                }
            if method == "GET" and path == "/admin/product-categories":
                parent = params.get("parent_category_id")
                if parent == "live-root":
                    return {
                        "product_categories": [
                            {
                                "id": "child-a",
                                "name": "A",
                                "handle": "a",
                                "is_active": True,
                                "parent_category_id": "live-root",
                            },
                            {
                                "id": "child-b",
                                "name": "B",
                                "handle": "b",
                                "is_active": True,
                                "parent_category_id": "live-root",
                            },
                        ],
                    }
                if parent == "child-a":
                    return {
                        "product_categories": [
                            {
                                "id": "child-a1",
                                "name": "A1",
                                "handle": "a1",
                                "is_active": True,
                                "parent_category_id": "child-a",
                            },
                        ],
                    }
                return {"product_categories": []}
            return {}

        self.request_mock.side_effect = fake_request

        root = self.adapter.fetch_tree("live-root")
        self.assertIsNotNone(root)
        self.assertEqual(root.external_id, "live-root")
        child_ids = {c.external_id for c in root.children}
        self.assertEqual(child_ids, {"child-a", "child-b"})
        child_a = next(c for c in root.children if c.external_id == "child-a")
        self.assertEqual(
            [c.external_id for c in child_a.children], ["child-a1"],
        )

    def test_fetch_tree_returns_none_when_no_root_pinned(self):
        self.assertIsNone(self.adapter.fetch_tree(None))
        self.request_mock.assert_not_called()

    def test_fetch_tree_returns_none_on_404(self):
        self.request_mock.side_effect = _http_error(404, "not found")
        self.assertIsNone(self.adapter.fetch_tree("missing"))

    # ── upsert_node create ──────────────────────────────────────────

    def test_upsert_create_posts_full_body(self):
        self.request_mock.return_value = {
            "product_category": {"id": "new-pcat"},
        }
        ext_id = self.adapter.upsert_node(
            external_id=None,
            parent_external_id="parent-pcat",
            name="Branch A",
            description="some desc",
            active=True,
            target_sales_channel=None,
        )
        self.assertEqual(ext_id, "new-pcat")

        # One call: POST /admin/product-categories with the full body.
        self.assertEqual(self.request_mock.call_count, 1)
        _, _, method, path = self.request_mock.call_args.args
        self.assertEqual(method, "POST")
        self.assertEqual(path, "/admin/product-categories")
        body = self.request_mock.call_args.kwargs["json"]
        self.assertEqual(body["name"], "Branch A")
        self.assertEqual(body["handle"], "branch-a")
        self.assertEqual(body["description"], "some desc")
        self.assertTrue(body["is_active"])
        self.assertEqual(body["parent_category_id"], "parent-pcat")

    # ── upsert_node update ──────────────────────────────────────────

    def test_upsert_update_posts_to_id_path(self):
        # A successful update returns whatever Medusa returns — we don't
        # consume the response; the caller's external_id stays authoritative.
        self.request_mock.return_value = {
            "product_category": {"id": "known-pcat"},
        }
        ext_id = self.adapter.upsert_node(
            external_id="known-pcat",
            parent_external_id="parent-pcat",
            name="Renamed",
            description="desc",
            active=False,
            target_sales_channel=None,
        )
        self.assertEqual(ext_id, "known-pcat")
        self.assertEqual(self.request_mock.call_count, 1)
        _, _, method, path = self.request_mock.call_args.args
        self.assertEqual(method, "POST")
        self.assertEqual(path, "/admin/product-categories/known-pcat")
        body = self.request_mock.call_args.kwargs["json"]
        self.assertEqual(body["name"], "Renamed")
        self.assertFalse(body["is_active"])
        self.assertEqual(body["handle"], "renamed")

    # ── upsert_node handle collision recovery ───────────────────────

    def test_upsert_create_recovers_from_handle_collision(self):
        """When the create returns "handle already exists", look it up by handle."""

        calls = []

        def fake_request(session, base_url, method, path, **kwargs):
            del session, base_url  # unused
            calls.append((method, path, kwargs))
            if method == "POST" and path == "/admin/product-categories":
                raise _http_error(422, '{"message":"handle already exists"}')
            if method == "GET" and path == "/admin/product-categories":
                # Lookup-by-handle returns the existing row.
                return {
                    "product_categories": [
                        {"id": "existing-pcat", "handle": "branch-a"},
                    ],
                }
            return {}

        self.request_mock.side_effect = fake_request

        ext_id = self.adapter.upsert_node(
            external_id=None,
            parent_external_id=None,
            name="Branch A",
            description=None,
            active=True,
            target_sales_channel=None,
        )
        self.assertEqual(ext_id, "existing-pcat")
        # We expect: 1 failed POST then 1 GET to look up by handle.
        methods = [(m, p) for (m, p, _) in calls]
        self.assertEqual(methods[0], ("POST", "/admin/product-categories"))
        self.assertEqual(methods[1], ("GET", "/admin/product-categories"))

    def test_upsert_update_falls_back_to_create_on_404(self):
        """Stale external_id (404 on update) should fall through to a create."""

        calls = []

        def fake_request(session, base_url, method, path, **kwargs):
            del session, base_url  # unused
            calls.append((method, path))
            if method == "POST" and path == "/admin/product-categories/stale":
                raise _http_error(404, "not found")
            if method == "POST" and path == "/admin/product-categories":
                return {"product_category": {"id": "fresh-pcat"}}
            return {}

        self.request_mock.side_effect = fake_request

        ext_id = self.adapter.upsert_node(
            external_id="stale",
            parent_external_id=None,
            name="Branch",
            description=None,
            active=True,
            target_sales_channel=None,
        )
        self.assertEqual(ext_id, "fresh-pcat")
        self.assertEqual(
            calls,
            [
                ("POST", "/admin/product-categories/stale"),
                ("POST", "/admin/product-categories"),
            ],
        )

    def test_upsert_ignores_target_sales_channel(self):
        """Medusa has no per-channel association — argument is accepted but ignored."""
        self.request_mock.return_value = {
            "product_category": {"id": "new-pcat"},
        }
        ext_id = self.adapter.upsert_node(
            external_id=None,
            parent_external_id=None,
            name="X",
            description=None,
            active=True,
            target_sales_channel="some-channel-id",
        )
        self.assertEqual(ext_id, "new-pcat")
        # Exactly one call — no sales-channel sub-call should have happened.
        self.assertEqual(self.request_mock.call_count, 1)
        body = self.request_mock.call_args.kwargs["json"]
        self.assertNotIn("sales_channel_id", body)
        self.assertNotIn("sales_channel_ids", body)

    # ── move_node ───────────────────────────────────────────────────

    def test_move_posts_parent_partial(self):
        self.request_mock.return_value = {"product_category": {"id": "node-pcat"}}
        self.adapter.move_node("node-pcat", "new-parent-pcat")
        self.assertEqual(self.request_mock.call_count, 1)
        _, _, method, path = self.request_mock.call_args.args
        self.assertEqual(method, "POST")
        self.assertEqual(path, "/admin/product-categories/node-pcat")
        body = self.request_mock.call_args.kwargs["json"]
        self.assertEqual(body, {"parent_category_id": "new-parent-pcat"})

    def test_move_to_root_passes_none_parent(self):
        self.request_mock.return_value = {}
        self.adapter.move_node("node-pcat", None)
        body = self.request_mock.call_args.kwargs["json"]
        self.assertEqual(body, {"parent_category_id": None})

    # ── delete_node ─────────────────────────────────────────────────

    def test_delete_swallows_404(self):
        self.request_mock.side_effect = _http_error(404, "not found")
        # Should not raise.
        self.adapter.delete_node("missing-pcat")

    def test_delete_propagates_other_errors(self):
        self.request_mock.side_effect = _http_error(500, "boom")
        with self.assertRaises(AdapterError):
            self.adapter.delete_node("pcat")

    # ── find_matching_nodes ─────────────────────────────────────────

    def test_find_matching_dedupes_overlapping_results(self):
        """Both ``q`` and ``handle`` queries return the same row — dedup by id."""

        def fake_request(session, base_url, method, path, **kwargs):
            del session, base_url, method, path  # unused
            params = kwargs.get("params") or {}
            if "q" in params:
                return {
                    "product_categories": [
                        {"id": "pcat-1", "name": "Furniture", "handle": "furniture"},
                        {"id": "pcat-2", "name": "Furniture (Old)", "handle": "furniture-old"},
                    ],
                }
            if "handle" in params:
                return {
                    "product_categories": [
                        # Overlaps with pcat-1 from the q query — must dedup.
                        {"id": "pcat-1", "name": "Furniture", "handle": "furniture"},
                    ],
                }
            return {"product_categories": []}

        self.request_mock.side_effect = fake_request

        matches = self.adapter.find_matching_nodes("Furniture", None)
        ids = [m.external_id for m in matches]
        # pcat-1 from name search, pcat-2 from name search — pcat-1 not
        # repeated even though the handle query returned it again.
        self.assertEqual(ids, ["pcat-1", "pcat-2"])

    def test_find_matching_returns_empty_for_empty_name(self):
        self.assertEqual(self.adapter.find_matching_nodes("", None), [])
        self.request_mock.assert_not_called()

    def test_find_matching_includes_parent_filter(self):
        """When a parent is supplied, both searches should constrain on it."""
        captured = []

        def fake_request(session, base_url, method, path, **kwargs):
            del session, base_url, method, path  # unused
            captured.append(kwargs.get("params") or {})
            return {"product_categories": []}

        self.request_mock.side_effect = fake_request

        self.adapter.find_matching_nodes("Branch", "parent-pcat")
        self.assertTrue(captured, "expected at least one search call")
        for params in captured:
            self.assertEqual(params.get("parent_category_id"), "parent-pcat")


if __name__ == "__main__":
    unittest.main()
