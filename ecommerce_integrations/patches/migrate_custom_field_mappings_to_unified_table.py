"""Consolidate Custom-Field-type rows from the legacy
``Shopware Setting.product_field_mappings`` table onto the unified
``item_custom_field_mappings`` table.

Before this patch, an operator configuring "map ERPNext field X to
Shopware customField Y" could end up in either of two places depending
on which push path they were thinking about:

- ``product_field_mappings`` (``Shopware Field Mapping``) fed the legacy
  variant-push path (``export/property_handler.py``) and the pull-side
  importer (``import_handlers/property_importer.py``) — via
  ``mapping_type == 'Custom Field'`` rows.
- ``item_custom_field_mappings`` (``Shopware Item Custom Field Mapping``)
  fed only the modern product-sync engine's single-item push
  (``product_sync/engine/canonical.py``).

A mapping configured in one table silently had no effect on the other
push path. This patch copies every enabled Custom-Field row from the
legacy table into the unified one (best-effort field_type derivation —
operators can refine afterwards) and removes the migrated rows from the
legacy table, since ``export/utils.py::get_field_mappings`` no longer
reads Custom-Field rows from there. Property / Standard Field rows are
left untouched.

Idempotent: only acts on rows still typed 'Custom Field' on the legacy
table; running twice finds nothing left to migrate the second time.
"""

import frappe

_SHOPWARE_TO_COERCION_TYPE = {
    "switch": "Boolean",
    "number": "Float",
    "text": "Text",
    "html": "Text",
    "select": "Text",
    "datetime": "Text",
}


def execute() -> None:
    if not frappe.db.exists("DocType", "Shopware Setting"):
        return
    if not frappe.db.exists("DocType", "Shopware Item Custom Field Mapping"):
        return
    if not frappe.db.exists("Custom Field", "Shopware Setting-item_custom_field_mappings"):
        # Custom Field not installed on this site yet — the
        # add_shopware_item_custom_field_mappings patch will create it;
        # re-run this migration afterwards (patches.txt order already
        # places this patch after that one).
        #
        # NOTE: Shopware Setting is a Single doctype — it has no
        # ``tabShopware Setting`` table, so ``frappe.db.has_column()``
        # (which was used here originally) raises TableMissingError
        # instead of returning False. Check for the Custom Field record
        # itself instead.
        return

    setting = frappe.get_single("Shopware Setting")
    legacy_rows = [
        row for row in (setting.get("product_field_mappings") or [])
        if row.mapping_type == "Custom Field" and row.enabled
    ]
    if not legacy_rows:
        return

    existing_item_fields = {
        row.item_field for row in (setting.get("item_custom_field_mappings") or [])
    }

    migrated = 0
    for row in legacy_rows:
        if not row.erpnext_field or not row.shopware_field:
            continue
        if row.erpnext_field in existing_item_fields:
            continue
        setting.append("item_custom_field_mappings", {
            "item_field": row.erpnext_field,
            "shopware_custom_field": row.shopware_field,
            "field_type": _SHOPWARE_TO_COERCION_TYPE.get(row.shopware_field_type, "Text"),
            "description": row.label_de or row.label_en or "",
        })
        existing_item_fields.add(row.erpnext_field)
        migrated += 1

    if not migrated:
        return

    # Drop the migrated rows from the legacy table — left in place
    # they'd look configured while silently doing nothing, which is
    # worse than being gone.
    remaining = [
        row for row in (setting.get("product_field_mappings") or [])
        if not (row.mapping_type == "Custom Field" and row.enabled)
    ]
    setting.set("product_field_mappings", remaining)
    setting.save(ignore_permissions=True)
    frappe.db.commit()  # noqa: SLF001

    try:
        from ecommerce_integrations.shopware6.export.utils import (
            invalidate_field_mappings_cache,
        )
        invalidate_field_mappings_cache()
    except Exception:  # noqa: BLE001
        pass
