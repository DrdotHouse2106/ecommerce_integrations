"""Install the ``Shopware Setting.item_custom_field_mappings`` table.

Adds a generic Item-field → Shopware-customField mapping table on
the Shopware Setting singleton so operators can wire up Idealo,
Google Shopping, age-rating or any other Shopware product
``customFields`` slot without code changes. The product-sync engine
reads the rows once per run (memoised on ``frappe.local``) and
hashes each mapped value into the canonical so a value flip on
the Item flips the per-section hash → drift → push.

Idempotent — skipped if the field already exists.
"""

from __future__ import annotations

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute() -> None:
    if not frappe.db.exists("DocType", "Shopware Setting"):
        return
    if not frappe.db.exists("DocType", "Shopware Item Custom Field Mapping"):
        # Doctype JSON hasn't been migrated yet on this site —
        # the child Doctype must exist before the Table field can
        # reference it as options. Re-running the patch after
        # ``bench migrate`` completes the install.
        return

    create_custom_fields({
        "Shopware Setting": [
            {
                "fieldname": "item_custom_field_mappings_section",
                "label": "Item → Shopware Custom Field Mapping",
                "fieldtype": "Section Break",
                "insert_after": "list_price_includes_tax",
                "collapsible": 1,
                "description": (
                    "Map Item DocType fields to Shopware product "
                    "``customFields`` keys. The product-sync engine "
                    "forwards each mapped value to the configured "
                    "Shopware slot on every push. Useful for "
                    "marketing-feed flags (Idealo, Google "
                    "Shopping), age ratings, custom labels — "
                    "anything Shopware's product-stream filters "
                    "or storefront templates need to read from a "
                    "customField key."
                ),
            },
            {
                "fieldname": "item_custom_field_mappings",
                "label": "Item Custom Field Mappings",
                "fieldtype": "Table",
                "options": "Shopware Item Custom Field Mapping",
                "insert_after": "item_custom_field_mappings_section",
            },
        ],
    }, ignore_validate=True)
    frappe.db.commit()  # noqa: SLF001
