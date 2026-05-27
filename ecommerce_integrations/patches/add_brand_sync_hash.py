"""Install ``Brand.last_synced_brand_hash`` custom field.

The product-sync engine's brand pre-flight (Phase A.5a in
``_apply_live``) compares this hash against the sha256 of the
brand's logo URL + description to decide whether to re-push the
``product_manufacturer`` row to Shopware. Without the field the
pre-flight runs every cron tick and re-uploads the logo bytes
unnecessarily — see ``CLAUDE.md`` § Delta-sync compatibility for
the contract this field exists to honour.

Idempotent — skipped if the field is already there.
"""

from __future__ import annotations

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute() -> None:
    if not frappe.db.exists("DocType", "Brand"):
        return
    create_custom_fields({
        "Brand": [
            {
                "fieldname": "last_synced_brand_hash",
                "label": "Last Synced Brand Hash",
                "fieldtype": "Data",
                "length": 64,
                "read_only": 1,
                "hidden": 1,
                "description": (
                    "sha256 of the brand's logo URL + description "
                    "as last successfully pushed to the active "
                    "ecommerce channel. The product-sync engine's "
                    "brand pre-flight compares this against a "
                    "freshly computed hash to decide whether to "
                    "re-upload the logo + upsert the "
                    "``product_manufacturer`` row. Clear to force "
                    "a re-push on the next sync."
                ),
            },
        ],
    }, ignore_validate=True)
    frappe.db.commit()  # noqa: SLF001
