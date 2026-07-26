"""Re-run add_shopware_item_custom_field_mappings again so its section
moves from "buried after list_price_includes_tax in the Preise section"
to right after the existing "Feld-Zuordnungen (erweitert)" section,
where operators actually look for field-mapping settings.

create_custom_fields(update=True) applies insert_after on already-
existing Custom Fields too (Frappe recomputes idx from insert_after on
save), so this is a straightforward reposition — no data is touched,
since the child rows are keyed by parent/parenttype/parentfield on
"Shopware Item Custom Field Mapping", independent of the parent field's
form position.

Idempotent.
"""

import frappe


def execute() -> None:
    if not frappe.db.exists("DocType", "Shopware Setting"):
        return
    from ecommerce_integrations.patches.add_shopware_item_custom_field_mappings import (
        execute as reinstall,
    )

    reinstall()
