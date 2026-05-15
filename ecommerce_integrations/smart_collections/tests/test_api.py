"""Whitelisted API surface tests for smart_collections/api.py.

``list_for_backend`` / ``toggle_target`` / ``preview`` are the JS-facing
entry points the Setting form widgets and collection-list buttons depend
on. ``adopt_match`` and ``preview_collection`` already have coverage in
``test_preview.py``; here we cover the remainder and the public-surface
guard.
"""

from __future__ import annotations

import unittest

import frappe
from frappe.tests import IntegrationTestCase

from ecommerce_integrations.smart_collections import api as smart_collections_api


class TestSmartCollectionsApiPublicSurface(unittest.TestCase):
    """The form's JS calls these by dotted path. Renaming or removing them
    is a breaking change for installed sites."""

    REQUIRED_CALLABLES = (
        "list_for_backend",
        "preview",
        "preview_collection",
        "adopt_match",
        "toggle_target",
    )

    def test_all_required_names_exist(self):
        for name in self.REQUIRED_CALLABLES:
            self.assertTrue(
                callable(getattr(smart_collections_api, name, None)),
                f"smart_collections.api.{name} must remain callable",
            )

    def test_all_endpoints_whitelisted(self):
        for name in self.REQUIRED_CALLABLES:
            fn = getattr(smart_collections_api, name)
            self.assertTrue(
                getattr(fn, "is_whitelisted", False)
                or getattr(fn, "_is_whitelisted", False)
                or getattr(fn, "whitelisted", False),
                f"smart_collections.api.{name} must be @frappe.whitelist()",
            )


class TestListForBackend(IntegrationTestCase):
    """``list_for_backend`` powers the Setting form's collection table.

    Rejects unknown backends; returns a flat list of target rows.
    """

    def test_rejects_unknown_backend(self):
        # Note: ``api.list_for_backend`` uses ``frappe.throw`` with the
        # default exception (ValidationError); the api wraps that as a
        # plain ValidationError so installed code can catch it.
        with self.assertRaises(Exception):
            smart_collections_api.list_for_backend("NotARealBackend")

    def test_accepts_known_backend_and_returns_list(self):
        rows = smart_collections_api.list_for_backend("Shopware")
        self.assertIsInstance(rows, list)


class TestToggleTargetValidation(IntegrationTestCase):
    """``toggle_target`` flips the ``enabled`` flag on a target row from
    the Setting widget. Permission is enforced through the parent doc's
    ``check_permission``; non-target docs must be rejected upfront."""

    def test_rejects_nonexistent_target(self):
        with self.assertRaises(Exception):
            smart_collections_api.toggle_target(
                target_id="not-a-real-target-row", enabled=1,
            )


if __name__ == "__main__":
    unittest.main()
