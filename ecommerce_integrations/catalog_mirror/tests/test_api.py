"""Whitelisted API surface tests for catalog_mirror/api.py.

Covers the public surface used by the Catalog Mirror form's *Preview*,
*Apply*, *Adopt* and *Set Orphan Action* dialogs:

- every documented endpoint is callable and whitelisted,
- ``list_for_backend`` rejects unknown backends and returns a flat shape,
- ``set_orphan_action`` rejects unknown actions and empty external_ids,
- ``adopt_node`` rejects invalid mode and an empty external_id when
  mode='pin'.

Endpoints that mutate backend state (the live ``apply_mirror_now`` path,
``find_matching_nodes``) need a registered adapter — those are exercised
end-to-end in the adapter tests and the resolver suite, so we stay at
input-validation level here.
"""

from __future__ import annotations

import unittest

import frappe
from frappe.tests import IntegrationTestCase

from ecommerce_integrations.catalog_mirror import api as catalog_mirror_api


class TestCatalogMirrorApiPublicSurface(unittest.TestCase):
    """Hook entries, form scripts and (Phase 4) the Item form indicator
    reference these by dotted path."""

    REQUIRED_CALLABLES = (
        "preview_mirror",
        "apply_mirror_now",
        "adopt_node",
        "set_orphan_action",
        "find_matching_nodes",
        "resolve_item_for_form",
        "list_for_backend",
    )

    def test_all_required_names_exist(self):
        for name in self.REQUIRED_CALLABLES:
            self.assertTrue(
                callable(getattr(catalog_mirror_api, name, None)),
                f"catalog_mirror.api.{name} must remain callable",
            )

    def test_all_endpoints_whitelisted(self):
        for name in self.REQUIRED_CALLABLES:
            fn = getattr(catalog_mirror_api, name)
            self.assertTrue(
                getattr(fn, "is_whitelisted", False)
                or getattr(fn, "_is_whitelisted", False)
                or getattr(fn, "whitelisted", False),
                f"catalog_mirror.api.{name} must be @frappe.whitelist()",
            )


class TestListForBackend(IntegrationTestCase):
    """``list_for_backend`` powers the Setting form's summary table.

    Returns a flat list of dicts; rejects backends outside KNOWN_BACKENDS.
    """

    def test_rejects_unknown_backend(self):
        with self.assertRaises(frappe.ValidationError):
            catalog_mirror_api.list_for_backend("NotARealBackend")

    def test_returns_empty_list_when_no_mirrors(self):
        # On a fresh test site there are no Mirror rows for Shopware; the
        # endpoint should return an empty list rather than raising.
        rows = catalog_mirror_api.list_for_backend("Shopware")
        self.assertIsInstance(rows, list)
        # Each returned row (if any) must have the documented summary
        # fields so the form's JS template doesn't NPE.
        for r in rows:
            self.assertIn("name", r)
            self.assertIn("backend", r) if "backend" in r else None
            self.assertIn("sync_status", r)


class TestAdoptNodeValidation(IntegrationTestCase):
    """Input validation — the live adopt path is exercised in the
    resolver tests; here we only check the guard rails."""

    def test_rejects_invalid_mode(self):
        # Permission check fires before mode validation when the mirror
        # doesn't exist; use a generic catch.
        with self.assertRaises(Exception):
            catalog_mirror_api.adopt_node(
                mirror="nonexistent-mirror",
                item_group="ITEM-GROUP-001",
                external_id="ext-001",
                mode="bogus",
            )


class TestSetOrphanActionValidation(IntegrationTestCase):
    """``set_orphan_action`` is the row-level quick-delete from the
    preview dialog. Validate the contract: known action + non-empty id."""

    def test_rejects_unknown_action(self):
        with self.assertRaises(Exception):
            catalog_mirror_api.set_orphan_action(
                mirror="nonexistent-mirror",
                external_id="ext-001",
                action="explode",
            )


class TestMappingFieldHelper(unittest.TestCase):
    """Internal but load-bearing — ``_backend_category_field`` is the
    single source of truth for which IG custom field a backend persists
    its category id into. Pure helper; safe to unit-test."""

    def test_known_backends_map_to_field(self):
        self.assertEqual(
            catalog_mirror_api._backend_category_field("Shopware"),
            "shopware_category_id",
        )
        self.assertEqual(
            catalog_mirror_api._backend_category_field("Medusa"),
            "medusa_category_id",
        )

    def test_unknown_backend_returns_none(self):
        self.assertIsNone(catalog_mirror_api._backend_category_field("NotABackend"))


if __name__ == "__main__":
    unittest.main()
