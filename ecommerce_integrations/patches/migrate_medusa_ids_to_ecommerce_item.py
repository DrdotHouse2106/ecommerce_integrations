"""Backfill Medusa external IDs from ``tabItem`` into ``tabEcommerce Item``.

Brings Medusa onto the canonical cross-integration mapping table
(``tabEcommerce Item`` with ``integration='medusa'``) — the same scheme
Shopware already uses. Until this patch runs, Medusa stores product /
variant ids in two custom columns on ``tabItem`` (``medusa_product_id``,
``medusa_variant_id``); reading those works as long as the columns
exist, but it locks Medusa out of the generic
``controllers/inventory.get_inventory_levels`` helper and forces
divergent adapter code paths (see
``smart_collections/engine/adapters/medusa.py`` vs ``shopware.py``).

Legacy storage layout — what the patch reads:

- Template ``Item`` rows carry ``medusa_product_id`` (no variant_id).
- Variant ``Item`` rows carry ``medusa_variant_id`` (their product_id
  lives on the parent template).
- Simple items carry both ``medusa_product_id`` and ``medusa_variant_id``.

Canonical layout — what the patch writes (one ``tabEcommerce Item`` row
per ``tabItem``, ``integration='medusa'``):

- Templates: ``has_variants=1``, ``integration_item_code=<pid>``,
  ``variant_id=NULL``, ``sku=NULL``.
- Variants: ``has_variants=0``, ``integration_item_code=<parent pid>``,
  ``variant_id=<vid>``, ``variant_of=<parent>``, ``sku=<item_code>``.
- Simple: ``has_variants=0``, ``integration_item_code=<pid>``,
  ``variant_id=<vid>``, ``sku=<item_code>``.

This patch is idempotent:

- No-op when Medusa is not installed on this site
  (``frappe.db.exists("DocType", "Medusa Setting")`` is False).
- No-op when the source columns are already gone.
- Re-running refreshes existing target rows in place (matched by
  ``integration='medusa'`` + ``erpnext_item_code``) and only inserts
  rows that don't yet have a target.
- Logs row counts so operators can confirm the migration before the
  follow-up patch drops the old columns.
"""

import frappe
from frappe.utils import now


def execute():
	if not frappe.db.exists("DocType", "Medusa Setting"):
		return

	if not frappe.db.table_exists("Ecommerce Item"):
		return

	# Guard: if the source columns were already dropped by a previous
	# run of the follow-up patch, there is nothing to migrate.
	cols = {
		r[0]
		for r in frappe.db.sql(
			"""SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS
			WHERE TABLE_SCHEMA = DATABASE()
			  AND TABLE_NAME = 'tabItem'
			  AND COLUMN_NAME IN ('medusa_product_id', 'medusa_variant_id')"""
		)
	}
	if "medusa_product_id" not in cols or "medusa_variant_id" not in cols:
		print("migrate_medusa_ids_to_ecommerce_item: source columns missing — nothing to do")
		return

	# Source set: every item that is somehow on Medusa, whether it
	# carries the product id directly (template / simple item) or just
	# the variant id (real variant under a template).
	source_count = frappe.db.sql(
		"""SELECT COUNT(*) FROM `tabItem`
		WHERE (medusa_product_id IS NOT NULL AND medusa_product_id != '')
		   OR (medusa_variant_id IS NOT NULL AND medusa_variant_id != '')"""
	)[0][0]
	if not source_count:
		print("migrate_medusa_ids_to_ecommerce_item: no source rows — nothing to do")
		return

	ts = now()

	# Pass 1: refresh rows that already exist for (integration='medusa',
	# erpnext_item_code=...). Variants inherit the product id from their
	# template via the LEFT JOIN ``parent``.
	frappe.db.sql(
		"""
		UPDATE `tabEcommerce Item` ei
		JOIN `tabItem` it ON it.name = ei.erpnext_item_code
		LEFT JOIN `tabItem` parent
		  ON parent.name = it.variant_of
		SET ei.integration_item_code = COALESCE(
		        NULLIF(it.medusa_product_id, ''),
		        NULLIF(parent.medusa_product_id, '')
		    ),
		    ei.variant_id   = NULLIF(it.medusa_variant_id, ''),
		    ei.has_variants = COALESCE(it.has_variants, 0),
		    ei.variant_of   = NULLIF(it.variant_of, ''),
		    ei.sku          = CASE WHEN COALESCE(it.has_variants, 0) = 1
		                           THEN NULL ELSE it.name END,
		    ei.modified     = %s,
		    ei.modified_by  = 'Administrator'
		WHERE ei.integration = 'medusa'
		  AND (
		      (it.medusa_product_id IS NOT NULL AND it.medusa_product_id != '')
		   OR (it.medusa_variant_id IS NOT NULL AND it.medusa_variant_id != '')
		  )
		""",
		(ts,),
	)

	# Pass 2: insert rows for items that don't yet have one.
	#
	# The PK is the ``name`` column; we derive it deterministically from
	# the item_code so a re-run finds the same row and the INSERT IGNORE
	# is a true no-op (Pass 1 above has already refreshed its data).
	#
	# ``LEFT JOIN parent`` covers the variant-without-its-own-product-id
	# case: variants get the parent's medusa_product_id as their
	# ``integration_item_code``.
	frappe.db.sql(
		"""
		INSERT IGNORE INTO `tabEcommerce Item` (
			name,
			creation, modified, modified_by, owner, docstatus, idx,
			integration, erpnext_item_code, integration_item_code,
			variant_id, has_variants, variant_of, sku,
			inventory_synced_on, item_synced_on
		)
		SELECT
			CONCAT('medusa-', LEFT(SHA2(it.name, 256), 24)),
			%s, %s, 'Administrator', 'Administrator', 0, 0,
			'medusa', it.name,
			COALESCE(
				NULLIF(it.medusa_product_id, ''),
				NULLIF(parent.medusa_product_id, '')
			),
			NULLIF(it.medusa_variant_id, ''),
			COALESCE(it.has_variants, 0),
			NULLIF(it.variant_of, ''),
			CASE WHEN COALESCE(it.has_variants, 0) = 1
			     THEN NULL ELSE it.name END,
			'1970-01-01 00:00:00',
			%s
		FROM `tabItem` it
		LEFT JOIN `tabItem` parent
		  ON parent.name = it.variant_of
		LEFT JOIN `tabEcommerce Item` ei
		  ON ei.integration = 'medusa'
		 AND ei.erpnext_item_code = it.name
		WHERE ei.name IS NULL
		  AND (
		      (it.medusa_product_id IS NOT NULL AND it.medusa_product_id != '')
		   OR (it.medusa_variant_id IS NOT NULL AND it.medusa_variant_id != '')
		  )
		""",
		(ts, ts, ts),
	)

	frappe.db.commit()

	# Verification + logging.
	migrated = frappe.db.sql(
		"SELECT COUNT(*) FROM `tabEcommerce Item` WHERE integration='medusa'"
	)[0][0]
	migrated_with_vid = frappe.db.sql(
		"""SELECT COUNT(*) FROM `tabEcommerce Item`
		WHERE integration='medusa'
		  AND variant_id IS NOT NULL AND variant_id != ''"""
	)[0][0]
	templates = frappe.db.sql(
		"""SELECT COUNT(*) FROM `tabEcommerce Item`
		WHERE integration='medusa' AND has_variants=1"""
	)[0][0]
	# How many source rows are still missing a target row?
	missing = frappe.db.sql(
		"""SELECT COUNT(*)
		FROM `tabItem` it
		LEFT JOIN `tabEcommerce Item` ei
		  ON ei.integration = 'medusa'
		 AND ei.erpnext_item_code = it.name
		WHERE ei.name IS NULL
		  AND (
		      (it.medusa_product_id IS NOT NULL AND it.medusa_product_id != '')
		   OR (it.medusa_variant_id IS NOT NULL AND it.medusa_variant_id != '')
		  )"""
	)[0][0]

	print(
		f"migrate_medusa_ids_to_ecommerce_item: source rows={source_count} "
		f"-> tabEcommerce Item(medusa)={migrated} "
		f"(templates={templates}, with variant_id={migrated_with_vid}, "
		f"still missing={missing})"
	)

	if missing:
		print(
			"migrate_medusa_ids_to_ecommerce_item: WARNING: "
			f"{missing} source rows did not get a target row. "
			"Investigate before letting drop_medusa_item_custom_fields run."
		)
