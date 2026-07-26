"""Re-run add_shopware_item_custom_field_mappings once more so its
Section Break's now-shortened description (redundancy with the
"Feld-Zuordnungen (erweitert)" section right above it removed) reaches
sites that already installed the longer version.

Unlike the earlier reposition fix, this only touches label/description
text — create_custom_fields(update=True) applies that to an
already-existing Custom Field via a plain .update()/.save(), no
delete-and-recreate needed (only insert_after/idx requires that).

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
