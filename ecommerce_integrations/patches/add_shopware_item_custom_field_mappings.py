"""Install (and, on re-run, reposition/re-label) the
``Shopware Setting.item_custom_field_mappings`` table.

Adds a generic Item-field → Shopware-customField mapping table on
the Shopware Setting singleton so operators can wire up Idealo,
Google Shopping, age-rating or any other Shopware product
``customFields`` slot without code changes. This is the single
unified mapping table: the product-sync engine's canonical builder
reads it directly for the modern single-item push
(``product_sync/engine/canonical.py::_get_dynamic_field_mappings``),
and ``export/utils.py::get_field_mappings`` folds the same rows in
for the legacy variant-push path and the pull-direction product
importer — see ``patches.migrate_custom_field_mappings_to_unified_table``
for the one-time data migration off the older, narrower
``product_field_mappings`` table.

Called both by the original one-time patch and by later re-run
patches (``translate_shopware_item_custom_field_mapping_section``,
``reposition_item_custom_field_mappings_section``) whenever the
label/description/position here changes — ``create_custom_fields``
with ``update=True`` applies label, description AND position
(``insert_after``) to an already-existing Custom Field, so re-running
is how a field ships a fix to sites that already installed it.

Idempotent — skipped if the child DocType doesn't exist yet.
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
                "label": "Zusatzfeld-Zuordnungen (Shopware Custom Fields)",
                "fieldtype": "Section Break",
                # Originally inserted after list_price_includes_tax (buried
                # in the Preise section, hard to find). Repositioned right
                # after the existing "Feld-Zuordnungen (erweitert)" section
                # so both mapping tables live next to each other.
                "insert_after": "field_mapping_info_html",
                "collapsible": 0,
                # Deliberately its own Section Break with NO depends_on
                # (unlike "Feld-Zuordnungen (erweitert)" right above it,
                # which requires upload_erpnext_items) — this table also
                # feeds the pull-direction product importer (Shopware →
                # ERPNext), which works independently of the push being
                # enabled. That's the *only* reason this is a separate
                # section rather than folded into the one above — keep
                # the description short, the section above already
                # explains why the split exists.
                "description": (
                    "Ein ERPNext-Artikelfeld immer auf denselben Shopware-"
                    "Zusatzfeld-Key abbilden — keine Property-Tabelle pro "
                    "Artikel nötig. Gilt für einfachen Artikel-Push, "
                    "Varianten-Push und Produkt-Import gleichermaßen."
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
