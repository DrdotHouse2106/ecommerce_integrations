"""Retroactively (re-)install the Catalog Mirror ``Item Group`` custom
fields on sites where ``setup_catalog_mirror`` is already recorded as
run in Patch Log but the DB columns never actually landed — e.g. an
earlier deploy attempt's ``bench migrate`` aborted on a later patch in
the list (``migrate_shopware_umbrellas_to_catalog_mirror`` walks the
live Shopware category tree and could throw before the SDK-response
attribute-access fixes landed) before this doctype's schema sync
completed, but the patch itself had already been marked done.

Symptom: ``(1054, "Unknown column 'shopware_category_id' in 'WHERE'")``
from the category importer / item-category linker / Catalog Mirror
itself, even though ``setup_catalog_mirror`` shows as already applied.

Safe to run unconditionally: ``create_custom_fields`` upserts by
fieldname, so this is a no-op on sites where the columns are already
present, and a normal patch-list entry (not a special case) means it
always runs exactly once per site — no reliance on the earlier,
possibly-incomplete run.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from ecommerce_integrations.patches.setup_catalog_mirror import CATALOG_MIRROR_CUSTOM_FIELDS


def execute() -> None:
    if not frappe.db.exists("DocType", "Item Group"):
        return
    create_custom_fields(CATALOG_MIRROR_CUSTOM_FIELDS, ignore_validate=True)
