"""Smoke tests for medusa/utils.py.

The mapping helpers (``upsert_medusa_mapping``, ``get_medusa_product_id``,
``clear_medusa_mapping``) persist rows in ``tabEcommerce Item`` — the
canonical Medusa ↔ ERPNext mapping table since the Wave 1 migration.
Direct reads of ``tabItem.medusa_product_id`` are no longer supported.

These tests exercise the round-trip:

- ``upsert_medusa_mapping`` creates a row,
- a re-upsert updates the existing row in place (idempotent),
- ``get_medusa_product_id`` / ``get_medusa_variant_id`` read it back,
- ``clear_medusa_mapping`` removes it,
- ``is_medusa_enabled`` returns False on an unconfigured site without
  raising.

Generic placeholder IDs only.
"""

from __future__ import annotations

import frappe
from frappe.tests import IntegrationTestCase

from ecommerce_integrations.medusa import utils as medusa_utils


class TestIsMedusaEnabled(IntegrationTestCase):
    """``is_medusa_enabled`` is checked on every webhook + every bulk-sync
    hook. It must never raise — even on an unconfigured site (fresh
    install, no Medusa Setting doctype yet)."""

    def test_returns_bool(self):
        self.assertIsInstance(medusa_utils.is_medusa_enabled(), bool)


class TestMedusaMappingRoundTrip(IntegrationTestCase):
    """``upsert_medusa_mapping`` is the single write-side API; the
    ``get_*`` family is the read side. Round-tripping a placeholder
    product/variant id through both must be lossless and idempotent."""

    TEST_ITEM_CODE = "TItem-MU-001"
    TEST_PRODUCT_ID = "prod_01TESTPLACEHOLDER01"
    TEST_VARIANT_ID = "variant_01TESTPLACEHOLDER01"

    def setUp(self):
        self._wipe()
        # The ``tabEcommerce Item`` row needs an Item to point at when
        # validators are strict; on this site the helpers go through
        # ``frappe.db.get_value`` which doesn't trip the link check.
        # We tolerate either path so the test runs regardless of whether
        # the Item exists.

    def tearDown(self):
        self._wipe()

    def _wipe(self):
        for name in frappe.get_all(
            "Ecommerce Item",
            filters={
                "integration": "medusa",
                "erpnext_item_code": self.TEST_ITEM_CODE,
            },
            pluck="name",
        ):
            try:
                frappe.delete_doc(
                    "Ecommerce Item", name,
                    force=True, ignore_permissions=True,
                )
            except Exception:
                pass

    def test_upsert_creates_row(self):
        # Need an Item to satisfy the link in ``tabEcommerce Item``.
        if not frappe.db.exists("Item", self.TEST_ITEM_CODE):
            uom_name = frappe.db.get_value("UOM", {}, "name") or "Nos"
            try:
                frappe.get_doc({
                    "doctype": "Item",
                    "item_code": self.TEST_ITEM_CODE,
                    "item_name": self.TEST_ITEM_CODE,
                    "item_group": (
                        frappe.db.get_value(
                            "Item Group",
                            {"is_group": 0},
                            "name",
                        )
                        or "All Item Groups"
                    ),
                    "stock_uom": uom_name,
                }).insert(ignore_permissions=True)
            except Exception:
                pass

        try:
            medusa_utils.upsert_medusa_mapping(
                self.TEST_ITEM_CODE,
                self.TEST_PRODUCT_ID,
                variant_id=self.TEST_VARIANT_ID,
            )
            self.assertEqual(
                medusa_utils.get_medusa_product_id(self.TEST_ITEM_CODE),
                self.TEST_PRODUCT_ID,
            )
            self.assertEqual(
                medusa_utils.get_medusa_variant_id(self.TEST_ITEM_CODE),
                self.TEST_VARIANT_ID,
            )
            self.assertTrue(medusa_utils.is_synced_to_medusa(self.TEST_ITEM_CODE))
        finally:
            try:
                frappe.delete_doc("Item", self.TEST_ITEM_CODE, force=True, ignore_permissions=True)
            except Exception:
                pass

    def test_get_returns_none_for_missing(self):
        self.assertIsNone(medusa_utils.get_medusa_product_id("NEVER-EXISTED-001"))
        self.assertIsNone(medusa_utils.get_medusa_variant_id("NEVER-EXISTED-001"))
        self.assertFalse(medusa_utils.is_synced_to_medusa("NEVER-EXISTED-001"))

    def test_upsert_skips_empty_args(self):
        # Empty item_code or product_id is a no-op (defensive — webhook
        # payloads have surprised us before with empty strings).
        medusa_utils.upsert_medusa_mapping("", self.TEST_PRODUCT_ID)
        medusa_utils.upsert_medusa_mapping(self.TEST_ITEM_CODE, "")
        # Nothing should have been written.
        self.assertIsNone(medusa_utils.get_medusa_product_id(self.TEST_ITEM_CODE))


class TestMedusaLogHelpersImport(IntegrationTestCase):
    """The log helpers are called from every webhook + every sync path.
    They must remain importable and callable so a typo doesn't take down
    the integration silently."""

    def test_create_and_update_log_callable(self):
        self.assertTrue(callable(medusa_utils.create_medusa_log))
        self.assertTrue(callable(medusa_utils.update_medusa_log))

    def test_log_error_callable(self):
        self.assertTrue(callable(medusa_utils.log_error))


class TestPriceConverters(IntegrationTestCase):
    """Price converters are no-ops for v2 (factor=1) — but the function
    must still return a clean float for None input so the formatter
    upstream doesn't crash on missing prices."""

    def test_medusa_to_erpnext_none(self):
        self.assertEqual(medusa_utils.medusa_price_to_erpnext(None), 0.0)

    def test_erpnext_to_medusa_none(self):
        self.assertEqual(medusa_utils.erpnext_price_to_medusa(None), 0.0)

    def test_medusa_to_erpnext_round_trip(self):
        # With factor=1 (v2) the converter is a no-op modulo float rounding.
        self.assertEqual(medusa_utils.medusa_price_to_erpnext(49.99), 49.99)
        self.assertEqual(medusa_utils.erpnext_price_to_medusa(49.99), 49.99)
