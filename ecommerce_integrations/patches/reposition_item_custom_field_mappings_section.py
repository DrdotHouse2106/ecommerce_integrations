"""Actually move the "Zusatzfeld-Zuordnungen" section from "buried after
list_price_includes_tax in the Preise section" to right after the
existing "Feld-Zuordnungen (erweitert)" section.

The previous attempt at this (this same patch, calling
``add_shopware_item_custom_field_mappings.execute()``, which just
updates the existing Custom Field's ``insert_after`` and saves) does
NOT work: Frappe's ``CustomField`` only recomputes ``idx`` from
``insert_after`` when ``self.is_new()`` — updating ``insert_after`` on
an already-existing Custom Field and saving leaves its position
untouched. That's why operators still couldn't find the section after
the previous patch ran.

The actual fix: delete the two Custom Field records and let
``add_shopware_item_custom_field_mappings.execute()`` recreate them
from scratch, so ``is_new()`` is true and the insert_after → idx
computation fires. This is safe for data — the child-table rows for
``item_custom_field_mappings`` live in
``tabShopware Item Custom Field Mapping``, keyed by
parent/parenttype/parentfield, entirely independent of the Custom
Field schema record. ``CustomField.on_trash()`` only removes property
setters and DocType Layout references; it never touches child-table
rows (confirmed against frappe/frappe's source). The rows reappear as
soon as the Table field is redeclared under the same fieldname.

Idempotent — re-running finds nothing to delete the second time
(fields already correctly positioned) and just re-confirms via
``add_shopware_item_custom_field_mappings.execute()``.
"""

import frappe


def execute() -> None:
    if not frappe.db.exists("DocType", "Shopware Setting"):
        return

    for fieldname in ("item_custom_field_mappings", "item_custom_field_mappings_section"):
        name = f"Shopware Setting-{fieldname}"
        if frappe.db.exists("Custom Field", name):
            frappe.delete_doc("Custom Field", name, ignore_permissions=True)

    frappe.db.commit()  # noqa: SLF001
    frappe.clear_cache(doctype="Shopware Setting")

    from ecommerce_integrations.patches.add_shopware_item_custom_field_mappings import (
        execute as reinstall,
    )

    reinstall()
