"""Integration tests for the Medusa ↔ ERPNext mapping migration (Track B).

Exercises the canonical mapping in ``tabEcommerce Item``
(``integration='medusa'``) end-to-end, without mocking the database:

- ``utils.get_medusa_product_id`` reads back what was written.
- The Smart Collections adapter's ``_items_to_medusa_product_ids``
  resolves variants to their template's Medusa product id when the
  variant itself has no row (parity with the Shopware adapter).
- Items with no mapping are returned in the ``missing`` set, not
  silently dropped.

The tests create their own Items + Ecommerce Item rows and clean up
in ``tearDown`` so they're safe to run repeatedly on the same site.
"""

import frappe
from frappe.tests import IntegrationTestCase

from ecommerce_integrations.medusa.utils import (
	clear_medusa_mapping,
	get_medusa_product_id,
	is_synced_to_medusa,
	upsert_medusa_mapping,
)
from ecommerce_integrations.smart_collections.engine.adapters.medusa import (
	_items_to_medusa_product_ids,
)


# Placeholders (per the generic-plugin rule — no real identifiers).
TEMPLATE_CODE = "ITEM-TPL-TEST-001"
VARIANT_CODE_A = "ITEM-TPL-TEST-001-A"
VARIANT_CODE_B = "ITEM-TPL-TEST-001-B"
SIMPLE_CODE = "ITEM-TEST-002"
UNMAPPED_CODE = "ITEM-TEST-003"

# Synthetic Medusa ids — never need to match a real Medusa product.
PROD_ID_TEMPLATE = "prod_test_template_001"
PROD_ID_SIMPLE = "prod_test_simple_002"
VARIANT_ID_A = "variant_test_001_a"
VARIANT_ID_B = "variant_test_001_b"
VARIANT_ID_SIMPLE = "variant_test_002"


def _ensure_item(item_code: str, *, has_variants: int = 0, variant_of: str | None = None) -> str:
	"""Create a minimal ``tabItem`` row if one doesn't already exist.

	Uses an item group / UOM that every ERPNext site ships with.
	Returns the item's name.
	"""
	if frappe.db.exists("Item", item_code):
		return item_code

	doc = frappe.get_doc({
		"doctype": "Item",
		"item_code": item_code,
		"item_name": item_code,
		"item_group": "All Item Groups",
		"stock_uom": "Nos",
		"is_stock_item": 0,
		"has_variants": int(bool(has_variants)),
		"variant_of": variant_of,
	})
	doc.flags.from_integration = True
	doc.insert(ignore_permissions=True, ignore_mandatory=True)
	return doc.name


class TestMedusaStorageMigration(IntegrationTestCase):
	"""Verify the new ``tabEcommerce Item``-backed Medusa mapping."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		# Build the fixture graph once for the whole test class.
		_ensure_item(TEMPLATE_CODE, has_variants=1)
		_ensure_item(VARIANT_CODE_A, variant_of=TEMPLATE_CODE)
		_ensure_item(VARIANT_CODE_B, variant_of=TEMPLATE_CODE)
		_ensure_item(SIMPLE_CODE)
		_ensure_item(UNMAPPED_CODE)

		# Wipe any leftover mapping rows from a prior failed run.
		for code in (TEMPLATE_CODE, VARIANT_CODE_A, VARIANT_CODE_B, SIMPLE_CODE, UNMAPPED_CODE):
			clear_medusa_mapping(code)

	def setUp(self):
		# Fresh mapping rows per test so test order doesn't matter.
		upsert_medusa_mapping(
			erpnext_item_code=TEMPLATE_CODE,
			integration_item_code=PROD_ID_TEMPLATE,
			has_variants=1,
		)
		upsert_medusa_mapping(
			erpnext_item_code=VARIANT_CODE_A,
			integration_item_code=PROD_ID_TEMPLATE,
			variant_id=VARIANT_ID_A,
			has_variants=0,
			variant_of=TEMPLATE_CODE,
		)
		# Note: VARIANT_CODE_B intentionally has NO mapping row — it
		# must still resolve via its template's row (the parent-fallback
		# behaviour the Smart Collections adapter relies on).
		upsert_medusa_mapping(
			erpnext_item_code=SIMPLE_CODE,
			integration_item_code=PROD_ID_SIMPLE,
			variant_id=VARIANT_ID_SIMPLE,
			has_variants=0,
		)

	def tearDown(self):
		for code in (TEMPLATE_CODE, VARIANT_CODE_A, VARIANT_CODE_B, SIMPLE_CODE, UNMAPPED_CODE):
			clear_medusa_mapping(code)

	# ─── get_medusa_product_id ────────────────────────────────────

	def test_get_medusa_product_id_returns_value_for_simple_item(self):
		self.assertEqual(get_medusa_product_id(SIMPLE_CODE), PROD_ID_SIMPLE)
		self.assertTrue(is_synced_to_medusa(SIMPLE_CODE))

	def test_get_medusa_product_id_returns_value_for_template(self):
		self.assertEqual(get_medusa_product_id(TEMPLATE_CODE), PROD_ID_TEMPLATE)

	def test_get_medusa_product_id_returns_none_for_unmapped_item(self):
		self.assertIsNone(get_medusa_product_id(UNMAPPED_CODE))
		self.assertFalse(is_synced_to_medusa(UNMAPPED_CODE))

	# ─── Smart Collections variant→template fallback ──────────────

	def test_smart_collections_uses_template_for_variant_without_row(self):
		"""When a variant has no Ecommerce Item row of its own, the
		template's row's ``integration_item_code`` is used. Two variants
		of the same template collapse to a single product id (dedup)."""
		resolved, missing = _items_to_medusa_product_ids(
			[VARIANT_CODE_A, VARIANT_CODE_B]
		)
		self.assertEqual(missing, [])
		# Both variants point at the same template product id; dedup
		# means a single entry in the resolved list.
		self.assertEqual(resolved, [PROD_ID_TEMPLATE])

	def test_smart_collections_returns_simple_item_id(self):
		resolved, missing = _items_to_medusa_product_ids([SIMPLE_CODE])
		self.assertEqual(missing, [])
		self.assertEqual(resolved, [PROD_ID_SIMPLE])

	def test_smart_collections_reports_unmapped_item_as_missing(self):
		resolved, missing = _items_to_medusa_product_ids(
			[SIMPLE_CODE, UNMAPPED_CODE]
		)
		self.assertEqual(set(resolved), {PROD_ID_SIMPLE})
		self.assertEqual(missing, [UNMAPPED_CODE])

	def test_smart_collections_handles_empty_input(self):
		resolved, missing = _items_to_medusa_product_ids([])
		self.assertEqual(resolved, [])
		self.assertEqual(missing, [])

	# ─── upsert idempotency ───────────────────────────────────────

	def test_upsert_is_idempotent(self):
		# Re-upsert the same row twice — count must stay at one.
		upsert_medusa_mapping(
			erpnext_item_code=SIMPLE_CODE,
			integration_item_code=PROD_ID_SIMPLE,
			variant_id=VARIANT_ID_SIMPLE,
			has_variants=0,
		)
		upsert_medusa_mapping(
			erpnext_item_code=SIMPLE_CODE,
			integration_item_code=PROD_ID_SIMPLE,
			variant_id=VARIANT_ID_SIMPLE,
			has_variants=0,
		)
		rows = frappe.get_all(
			"Ecommerce Item",
			filters={"integration": "medusa", "erpnext_item_code": SIMPLE_CODE},
			pluck="name",
		)
		self.assertEqual(len(rows), 1)

	def test_upsert_updates_changed_fields_in_place(self):
		# Change the variant id on a re-upsert and confirm the row was
		# updated, not duplicated.
		new_variant_id = "variant_test_002_renamed"
		upsert_medusa_mapping(
			erpnext_item_code=SIMPLE_CODE,
			integration_item_code=PROD_ID_SIMPLE,
			variant_id=new_variant_id,
			has_variants=0,
		)
		mapping = frappe.db.get_value(
			"Ecommerce Item",
			{"integration": "medusa", "erpnext_item_code": SIMPLE_CODE},
			["integration_item_code", "variant_id"],
			as_dict=True,
		)
		self.assertEqual(mapping.integration_item_code, PROD_ID_SIMPLE)
		self.assertEqual(mapping.variant_id, new_variant_id)
