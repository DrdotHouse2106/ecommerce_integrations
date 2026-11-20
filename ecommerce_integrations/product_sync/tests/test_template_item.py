"""Description templates see custom Item fields also on bulk snapshots."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch


class _Snap(SimpleNamespace):
    def as_dict(self):
        return {"item_code": self.item_code, "description": "std"}


class TestTemplateItem(unittest.TestCase):
    def test_snapshot_is_expanded_with_full_item(self):
        import frappe

        from ecommerce_integrations.product_sync.engine.canonical import _template_item
        full = SimpleNamespace(as_dict=lambda: {"item_code": "A", "custom_html": "<p>x</p>", "description": "old"})
        with patch.object(frappe, "get_cached_doc", return_value=full):
            data = _template_item(_Snap(item_code="A", dynamic_field_values={}))
        self.assertEqual(data["custom_html"], "<p>x</p>")
        self.assertEqual(data["description"], "std")

    def test_real_doc_untouched(self):
        import frappe

        from ecommerce_integrations.product_sync.engine.canonical import _template_item
        doc = SimpleNamespace(as_dict=lambda: {"item_code": "A", "custom_html": "y"})
        with patch.object(frappe, "get_cached_doc") as g:
            self.assertEqual(_template_item(doc)["custom_html"], "y")
            g.assert_not_called()
