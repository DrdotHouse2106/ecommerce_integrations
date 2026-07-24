"""Tests for ``shopware6.import_handlers.product_importer``.

Per CLAUDE.md: real ERPNext DB (no DB mocking) for anything that
exercises the sync/matching pipeline — only the Shopware HTTP layer
would be mocked, and only where a test actually needs it (most of
these exercise the module's own DB-writing helpers directly, so no
Shopware client is involved at all).
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from ecommerce_integrations.shopware6.import_handlers import product_importer as pi
from ecommerce_integrations.shopware6.tests.utils import ShopwareTestCase


def _unique_sku(prefix: str) -> str:
    return f"{prefix}-{frappe.generate_hash(length=8)}"


class TestPureHelpers(unittest.TestCase):
    """No DB access — pure data-shape functions."""

    def test_build_criteria_shape(self):
        criteria = pi._build_criteria(page=3)
        self.assertEqual(criteria["page"], 3)
        self.assertEqual(criteria["limit"], pi._PAGE_SIZE)
        self.assertEqual(
            criteria["filter"], [{"type": "equals", "field": "parentId", "value": None}],
        )
        for key in ("tax", "media", "cover", "properties", "options", "prices", "categories", "manufacturer", "deliveryTime", "children"):
            self.assertIn(key, criteria["associations"])

    def test_resolve_categories_dedupes_and_skips_unmapped(self):
        product_data = {
            "categories": [
                {"id": "cat-a"}, {"id": "cat-a"}, {"id": "cat-b"}, {"id": "unknown"},
            ],
        }
        mapping = {"cat-a": "Group A", "cat-b": "Group B"}
        self.assertEqual(pi._resolve_categories(product_data, mapping), ["Group A", "Group B"])

    def test_split_primary_and_extra_with_categories(self):
        primary, extra = pi._split_primary_and_extra(["Group A", "Group B", "Group C"])
        self.assertEqual(primary, "Group A")
        self.assertEqual(extra, ["Group B", "Group C"])


class TestResolveOrCreateItem(ShopwareTestCase, IntegrationTestCase):
    """The non-destructive SKU-matching contract — the single most
    safety-critical property of this module."""

    def _stats(self):
        return {"created": 0, "variants_created": 0, "matched": 0, "errors": []}

    def test_creates_new_item_for_unknown_sku(self):
        sku = _unique_sku("PI-NEW")
        item_group = frappe.db.get_value("Item Group", {"is_group": 0}, "name")
        stats = self._stats()

        item_code, matched = pi._resolve_or_create_item(
            {
                "item_code": sku, "item_name": sku, "description": sku,
                "item_group": item_group, "has_variants": 0, "stock_uom": "Nos",
                "is_stock_item": 1, "disabled": 0,
            },
            product_id=f"sw-{sku}", variant_id=f"sw-{sku}", variant_of=None, has_variants=0,
            stats=stats,
        )

        self.assertEqual(item_code, sku)
        self.assertFalse(matched)
        self.assertEqual(stats["created"], 1)
        self.assertTrue(frappe.db.exists("Item", sku))
        self.assertTrue(frappe.db.exists(
            "Ecommerce Item", {"integration": "shopware6", "erpnext_item_code": sku},
        ))

    def test_matches_existing_item_without_touching_item_group(self):
        sku = _unique_sku("PI-EXIST")
        original_group = frappe.db.get_value("Item Group", {"is_group": 0}, "name")
        frappe.get_doc({
            "doctype": "Item", "item_code": sku, "item_name": "Pre-existing WeClapp item",
            "item_group": original_group, "is_stock_item": 1, "stock_uom": "Nos",
        }).insert(ignore_permissions=True)

        stats = self._stats()
        different_group = frappe.db.get_value(
            "Item Group", {"is_group": 0, "name": ["!=", original_group]}, "name",
        ) or original_group

        item_code, matched = pi._resolve_or_create_item(
            {
                "item_code": sku, "item_name": "Should not overwrite", "description": "x",
                "item_group": different_group, "has_variants": 0, "stock_uom": "Nos",
                "is_stock_item": 1, "disabled": 0,
            },
            product_id=f"sw-{sku}", variant_id=f"sw-{sku}", variant_of=None, has_variants=0,
            stats=stats,
        )

        self.assertEqual(item_code, sku)
        self.assertTrue(matched)
        self.assertEqual(stats["matched"], 1)
        self.assertEqual(stats["created"], 0)
        # The existing Item's own fields must be untouched.
        self.assertEqual(frappe.db.get_value("Item", sku, "item_group"), original_group)
        self.assertEqual(frappe.db.get_value("Item", sku, "item_name"), "Pre-existing WeClapp item")
        self.assertTrue(frappe.db.exists(
            "Ecommerce Item", {"integration": "shopware6", "erpnext_item_code": sku},
        ))

    def test_variant_of_mismatch_logs_but_still_links(self):
        sku = _unique_sku("PI-VARMISMATCH")
        item_group = frappe.db.get_value("Item Group", {"is_group": 0}, "name")
        # Pre-existing standalone item (no variant_of) with this SKU.
        frappe.get_doc({
            "doctype": "Item", "item_code": sku, "item_name": "Standalone item",
            "item_group": item_group, "is_stock_item": 1, "stock_uom": "Nos",
        }).insert(ignore_permissions=True)

        stats = self._stats()
        item_code, matched = pi._resolve_or_create_item(
            {
                "item_code": sku, "item_name": "x", "description": "x",
                "item_group": item_group, "has_variants": 0, "stock_uom": "Nos",
                "is_stock_item": 1, "variant_of": "SOME-TEMPLATE",
            },
            product_id=f"sw-{sku}", variant_id=f"sw-{sku}", variant_of="SOME-TEMPLATE", has_variants=0,
            stats=stats,
        )

        self.assertEqual(item_code, sku)
        self.assertTrue(matched)
        self.assertTrue(stats["errors"])
        # Never reparented onto the mismatched template.
        self.assertNotEqual(frappe.db.get_value("Item", sku, "variant_of"), "SOME-TEMPLATE")


class TestLinkCategories(ShopwareTestCase, IntegrationTestCase):
    def test_never_touches_primary_item_group_only_appends(self):
        sku = _unique_sku("PI-CATLINK")
        primary_group = frappe.db.get_value("Item Group", {"is_group": 0}, "name")
        other_groups = frappe.get_all(
            "Item Group", filters={"is_group": 0, "name": ["!=", primary_group]}, limit=2, pluck="name",
        )
        if not other_groups:
            self.skipTest("Site needs at least two leaf Item Groups for this test")

        frappe.get_doc({
            "doctype": "Item", "item_code": sku, "item_name": sku,
            "item_group": primary_group, "is_stock_item": 1, "stock_uom": "Nos",
        }).insert(ignore_permissions=True)

        stats = {"category_links_added": 0}
        pi._link_categories(sku, other_groups, stats)

        item = frappe.get_doc("Item", sku)
        self.assertEqual(item.item_group, primary_group)
        linked = {row.item_group for row in item.additional_item_groups}
        self.assertTrue(set(other_groups).issubset(linked))
        self.assertGreater(stats["category_links_added"], 0)


class TestPrices(ShopwareTestCase, IntegrationTestCase):
    def test_upsert_creates_then_updates(self):
        sku = _unique_sku("PI-PRICE")
        item_group = frappe.db.get_value("Item Group", {"is_group": 0}, "name")
        frappe.get_doc({
            "doctype": "Item", "item_code": sku, "item_name": sku,
            "item_group": item_group, "is_stock_item": 1, "stock_uom": "Nos",
        }).insert(ignore_permissions=True)
        price_list = frappe.db.get_value("Price List", {"selling": 1}, "name")
        if not price_list:
            self.skipTest("Site needs a selling Price List for this test")

        pi._upsert_item_price(sku, price_list, 42.5)
        rate = frappe.db.get_value(
            "Item Price", {"item_code": sku, "price_list": price_list, "selling": 1}, "price_list_rate",
        )
        self.assertEqual(float(rate), 42.5)

        pi._upsert_item_price(sku, price_list, 55.0)
        rate = frappe.db.get_value(
            "Item Price", {"item_code": sku, "price_list": price_list, "selling": 1}, "price_list_rate",
        )
        self.assertEqual(float(rate), 55.0)
        # Still exactly one row — no duplicate created on update.
        count = frappe.db.count("Item Price", {"item_code": sku, "price_list": price_list, "selling": 1})
        self.assertEqual(count, 1)


class TestImages(ShopwareTestCase, IntegrationTestCase):
    def test_gallery_images_stored_as_remote_url_no_download(self):
        sku = _unique_sku("PI-IMG")
        item_group = frappe.db.get_value("Item Group", {"is_group": 0}, "name")
        frappe.get_doc({
            "doctype": "Item", "item_code": sku, "item_name": sku,
            "item_group": item_group, "is_stock_item": 1, "stock_uom": "Nos",
        }).insert(ignore_permissions=True)

        data = {
            "cover": {"media": {"id": "media-1", "url": "https://cdn.example.com/cover.jpg"}},
            "media": [
                {"position": 0, "media": {"id": "media-1", "url": "https://cdn.example.com/cover.jpg"}},
                {"position": 1, "media": {"id": "media-2", "url": "https://cdn.example.com/gallery-1.jpg"}},
            ],
        }
        stats = {"images_set": 0}
        pi._write_images(sku, data, stats)

        self.assertEqual(frappe.db.get_value("Item", sku, "image"), "https://cdn.example.com/cover.jpg")
        self.assertEqual(stats["images_set"], 1)
        self.assertTrue(frappe.db.exists("File", {
            "attached_to_doctype": "Item", "attached_to_name": sku,
            "file_url": "https://cdn.example.com/gallery-1.jpg",
        }))
        # No download attempted — url just stored as-is (is_private=0,
        # points at Shopware's own CDN).
        file_doc = frappe.get_doc("File", {
            "attached_to_doctype": "Item", "attached_to_name": sku,
            "file_url": "https://cdn.example.com/gallery-1.jpg",
        })
        self.assertEqual(file_doc.is_private, 0)


class TestSkipShopwareSyncRestored(ShopwareTestCase, IntegrationTestCase):
    """frappe.flags.skip_shopware_sync must be restored even when the
    run fails partway — otherwise every subsequent Item save in the
    same worker process would silently stop pushing to Shopware."""

    @patch("ecommerce_integrations.shopware6.import_handlers.product_importer.get_shopware_client")
    @patch("ecommerce_integrations.shopware6.import_handlers.product_importer.create_shopware_log")
    @patch("ecommerce_integrations.shopware6.import_handlers.product_importer.update_shopware_log")
    def test_flag_restored_after_exception(self, mock_update_log, mock_create_log, mock_get_client):
        mock_get_client.side_effect = RuntimeError("boom")
        frappe.flags.skip_shopware_sync = False

        with self.assertRaises(RuntimeError):
            pi._run_product_import("fake-log-name")

        self.assertFalse(frappe.flags.skip_shopware_sync)
        mock_update_log.assert_called_with(
            "fake-log-name", status="Error", exception="boom",
        )
