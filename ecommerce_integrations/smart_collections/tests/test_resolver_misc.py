"""Resolver tests for Manufacturer, Brand, Item Field and Stock rules."""

import frappe
from frappe.tests import IntegrationTestCase

from ecommerce_integrations.smart_collections.engine.resolver import resolve
from ecommerce_integrations.smart_collections.tests.test_helpers import (
    cleanup_test_data,
    make_item,
)


_TEST_MANUFACTURER = "TestMfr-Smart-A"
_TEST_OTHER_MFR = "TestMfr-Smart-B"


def _ensure_manufacturer(name: str) -> None:
    if not frappe.db.exists("Manufacturer", name):
        frappe.get_doc({"doctype": "Manufacturer", "short_name": name}).insert(
            ignore_permissions=True
        )


def _delete_manufacturer(name: str) -> None:
    if frappe.db.exists("Manufacturer", name):
        try:
            frappe.delete_doc(
                "Manufacturer", name, force=True, ignore_permissions=True
            )
        except Exception:
            pass


class TestResolverMisc(IntegrationTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cleanup_test_data()
        _ensure_manufacturer(_TEST_MANUFACTURER)
        _ensure_manufacturer(_TEST_OTHER_MFR)
        cls.with_mfr_a = make_item("TItem-MI-1", manufacturer=_TEST_MANUFACTURER)
        cls.with_mfr_b = make_item("TItem-MI-2", manufacturer=_TEST_OTHER_MFR)
        cls.no_mfr = make_item("TItem-MI-3")

    @classmethod
    def tearDownClass(cls):
        cleanup_test_data()
        _delete_manufacturer(_TEST_MANUFACTURER)
        _delete_manufacturer(_TEST_OTHER_MFR)
        super().tearDownClass()

    def _coll(self, rules):
        return frappe.get_doc(
            {
                "doctype": "Ecommerce Smart Collection",
                "title": "Resolver Misc Test",
                "slug": f"resolver-misc-test-{frappe.generate_hash(length=6)}",
                "rule_combinator": "AND",
                "rules": rules,
                "targets": [],
            }
        )

    def test_manufacturer_in(self):
        coll = self._coll(
            [
                {
                    "rule_type": "Manufacturer",
                    "operator": "in",
                    "value": _TEST_MANUFACTURER,
                }
            ]
        )
        self.assertEqual(resolve(coll), {self.with_mfr_a})

    def test_manufacturer_not_in_excludes_null(self):
        coll = self._coll(
            [
                {
                    "rule_type": "Manufacturer",
                    "operator": "not_in",
                    "value": _TEST_MANUFACTURER,
                }
            ]
        )
        result = resolve(coll)
        self.assertIn(self.with_mfr_b, result)
        self.assertNotIn(self.with_mfr_a, result)
        self.assertNotIn(
            self.no_mfr,
            result,
            "Items with NULL manufacturer must not match not_in (strict NULL)",
        )

    def test_item_field_regex(self):
        coll = self._coll(
            [
                {
                    "rule_type": "Item Field",
                    "field_key": "item_code",
                    "operator": "regex",
                    "value": "^TItem-MI-1$",
                }
            ]
        )
        self.assertEqual(resolve(coll), {self.with_mfr_a})

    def test_item_field_whitelist_blocks_unknown_field(self):
        coll = self._coll(
            [
                {
                    "rule_type": "Item Field",
                    "field_key": "description",
                    "operator": "equals",
                    "value": "anything",
                }
            ]
        )
        with self.assertRaises(ValueError):
            resolve(coll)

    def test_stock_is_empty_matches_when_no_bin_rows(self):
        # The reference site has zero rows in tabBin, so every test item is
        # is_empty. This guards against regressions where the COALESCE(SUM(..))
        # subquery returns NULL and the comparison silently flips.
        coll = self._coll(
            [{"rule_type": "Stock", "operator": "is_empty", "value": ""}]
        )
        result = resolve(coll)
        self.assertIn(self.with_mfr_a, result)
        self.assertIn(self.with_mfr_b, result)
        self.assertIn(self.no_mfr, result)
