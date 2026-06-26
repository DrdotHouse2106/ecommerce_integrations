"""Install custom fields the Catalog Mirror feature depends on.

Adds five fields to ``Item Group``:

- ``shopware_category_id`` — persistent Shopware category UUID for the IG.
- ``medusa_category_id`` — Medusa product-category UUID for the IG.
- ``catalog_mirror_skip`` — operator opt-out for an IG (and its subtree).
- ``shopware_category_hash`` — last-synced canonical hash (Shopware).
- ``medusa_category_hash`` — last-synced canonical hash (Medusa).

The ``*_category_hash`` fields back the mirror's delta layer: the
canonical fingerprint (name + description + SEO meta + active) recorded
after a successful push, so an unchanged node is a hash-confirmed noop on
the next run. They are informational mirrors of internal state — the
live-diff stays authoritative for backend drift.

The doctype is registered through the module's ``modules.txt`` entry —
no reload_doc here for the doctype itself; ``bench migrate`` discovers
the JSON on its own. This patch only owns the custom-field additions.

Idempotent: ``create_custom_fields`` upserts by ``fieldname`` so a
second run is a no-op. The patch is also no-op safe — when the
``Catalog Mirror`` module isn't installed on this site (e.g. an
operator who installs a slimmer subset) the patch is still cheap
because the custom fields are useful even without the doctype.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


CATALOG_MIRROR_CUSTOM_FIELDS = {
    "Item Group": [
        {
            "fieldname": "shopware_category_id",
            "label": "Shopware Category ID",
            "fieldtype": "Data",
            "read_only": 1,
            "no_copy": 1,
            "insert_after": "image",
            "description": (
                "Backend Shopware category UUID for this Item Group. "
                "Written by Catalog Mirror; do not edit manually."
            ),
        },
        {
            "fieldname": "medusa_category_id",
            "label": "Medusa Category ID",
            "fieldtype": "Data",
            "read_only": 1,
            "no_copy": 1,
            "insert_after": "shopware_category_id",
            "description": (
                "Backend Medusa product-category UUID for this Item Group. "
                "Written by Catalog Mirror; do not edit manually."
            ),
        },
        {
            "fieldname": "catalog_mirror_skip",
            "label": "Skip in Catalog Mirror",
            "fieldtype": "Check",
            "default": "0",
            "insert_after": "medusa_category_id",
            "description": (
                "When checked, this Item Group and its descendants are "
                "excluded from every Catalog Mirror walk."
            ),
        },
        {
            "fieldname": "shopware_category_hash",
            "label": "Shopware Category Hash",
            "fieldtype": "Data",
            "read_only": 1,
            "no_copy": 1,
            "hidden": 1,
            "insert_after": "shopware_category_id",
            "description": (
                "Last-synced canonical hash for the Shopware mirror. "
                "Written by Catalog Mirror; do not edit manually."
            ),
        },
        {
            "fieldname": "medusa_category_hash",
            "label": "Medusa Category Hash",
            "fieldtype": "Data",
            "read_only": 1,
            "no_copy": 1,
            "hidden": 1,
            "insert_after": "medusa_category_id",
            "description": (
                "Last-synced canonical hash for the Medusa mirror. "
                "Written by Catalog Mirror; do not edit manually."
            ),
        },
    ],
}


def execute() -> None:
    # No-op safe: the Item Group doctype ships with ERPNext, but a
    # site without the table at all (vanilla Frappe install) should
    # still let the patch list run cleanly.
    if not frappe.db.exists("DocType", "Item Group"):
        return
    create_custom_fields(CATALOG_MIRROR_CUSTOM_FIELDS, ignore_validate=True)
