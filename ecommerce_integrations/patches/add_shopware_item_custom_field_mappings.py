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
                "label": "Artikel → Shopware Zusatzfeld-Zuordnung",
                "fieldtype": "Section Break",
                "insert_after": "list_price_includes_tax",
                "collapsible": 1,
                "description": (
                    "Artikel-Felder auf Shopware-Produkt-``customFields``-"
                    "Keys abbilden. Das Produkt-Sync liest jede Zeile "
                    "einmal pro Lauf und überträgt den zugeordneten Wert "
                    "bei jedem Push in den konfigurierten Shopware-Slot. "
                    "Nützlich für Marketing-Feed-Flags (Idealo, Google "
                    "Shopping), Altersfreigaben, eigene Kennzeichnungen — "
                    "alles, was Shopwares Produkt-Stream-Filter oder "
                    "Storefront-Templates aus einem customField-Key lesen "
                    "sollen."
                ),
            },
            {
                "fieldname": "item_custom_field_mappings",
                "label": "Zusatzfeld-Zuordnungen",
                "fieldtype": "Table",
                "options": "Shopware Item Custom Field Mapping",
                "insert_after": "item_custom_field_mappings_section",
            },
        ],
    }, ignore_validate=True, update=True)
    frappe.db.commit()  # noqa: SLF001
