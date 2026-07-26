"""Force-fix the "Zusatzfeld-Zuordnungen" section position for sites
that already consumed ``reposition_item_custom_field_mappings_section``.

That patch tried to reposition the section by updating ``insert_after``
on the already-existing Custom Field and saving — which is a no-op:
Frappe's ``CustomField`` only recomputes ``idx`` from ``insert_after``
when ``self.is_new()``. Since the patch already ran once (and Frappe's
Patch Log never retries a patch that returned without raising), simply
fixing the logic in-place doesn't reach sites that already executed
it — hence this separate, new one-time patch with the corrected
delete-and-recreate approach.

Safe for data: see the docstring on
``reposition_item_custom_field_mappings_section`` for why deleting and
recreating the Custom Field records doesn't touch the underlying
``tabShopware Item Custom Field Mapping`` rows.

Idempotent.
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
