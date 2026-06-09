"""Install ``Item.is_configurable`` and wire it to both backends.

Adds a Check field to Item the operator can toggle per product to
indicate "this product is configurable via the storefront's runtime
configurator service". Drives a "Konfigurieren" CTA badge on the
PDP — the badge needs a cheap per-product boolean, the full config
runs against a resolver at runtime.

The custom field flows through the existing delta-sync pipeline:

* Canonical: ``basic.is_configurable`` carries the boolean, hashed
  with the rest of the basic section so a checkbox flip flips the
  per-item hash → next sync re-pushes.
* Shopware: written via the dynamic
  ``Shopware Setting.item_custom_field_mappings`` table seeded by
  this patch (``is_configurable`` → ``customFields.is_configurable``,
  Boolean coercion). Operator can rename the Shopware-side key in
  the UI later if their custom-field-set uses a different
  convention (``kreckler_is_configurable``, ``shop_is_configurable``,
  …) — the change takes effect on the next sync without code edits.
* Medusa: ``metadata.is_configurable`` (true/false) emitted by
  ``build_medusa_payload`` from ``canonical.basic.is_configurable``.

Idempotent:

* The Custom Field create is skipped when the field already exists.
* The mapping seed is skipped when a row with the same
  ``item_field`` already lives in
  ``Shopware Setting.item_custom_field_mappings``.

No-op safe: skipped entirely when the integration isn't installed
on this site.
"""

from __future__ import annotations

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


ITEM_FIELD = "is_configurable"
SHOPWARE_KEY = "is_configurable"


def execute() -> None:
    if not frappe.db.exists("DocType", "Item"):
        return

    if not frappe.db.has_column("Item", ITEM_FIELD):
        create_custom_fields({
            "Item": [
                {
                    "fieldname": ITEM_FIELD,
                    "label": "Configurable in Storefront",
                    "fieldtype": "Check",
                    "default": "0",
                    # Land near the existing ecommerce flags so an
                    # operator scanning the Item form finds them
                    # grouped. ``is_sales_item`` is a Frappe-stock
                    # field that's always present; anchoring there
                    # avoids a fragile insert_after on a custom
                    # field that may or may not exist on a given
                    # site.
                    "insert_after": "is_sales_item",
                    "description": (
                        "When checked, the storefront PDP renders a "
                        "'Konfigurieren' CTA badge that opens the "
                        "runtime configurator. Set only on products "
                        "the configurator's resolver actually "
                        "supports — incorrect values surface as 404s "
                        "from the resolver after the user clicks."
                    ),
                },
            ],
        })

    if not frappe.db.exists("DocType", "Shopware Setting"):
        return
    if not frappe.db.exists("DocType", "Shopware Item Custom Field Mapping"):
        return

    settings = frappe.get_single("Shopware Setting")
    existing = {
        (row.item_field or "").strip()
        for row in (getattr(settings, "item_custom_field_mappings", None) or [])
    }
    if ITEM_FIELD in existing:
        return

    settings.append("item_custom_field_mappings", {
        "item_field": ITEM_FIELD,
        "shopware_custom_field": SHOPWARE_KEY,
        "field_type": "Boolean",
        "description": (
            "Per-product configurator-eligibility flag. Storefront "
            "reads product.customFields.is_configurable to render the "
            "Konfigurieren CTA badge."
        ),
    })
    settings.save(ignore_permissions=True)
