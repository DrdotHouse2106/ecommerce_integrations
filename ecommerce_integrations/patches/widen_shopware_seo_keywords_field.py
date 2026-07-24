"""Widen Item Group.seo_keywords from "Data" (140 char cap) to "Small
Text" (no limit) on already-migrated sites.

Shopware's category ``keywords`` field is a comma-separated list that
routinely exceeds 140 characters. With the field still typed "Data",
saving any Item Group whose imported keywords were longer than that
failed outright ("... wird abgeschnitten werden, da maximal 140
Zeichen erlaubt sind") instead of just truncating — breaking the
category import for every affected category.

Idempotent: create_custom_fields (called inside setup_custom_fields)
upserts by fieldname, including fieldtype changes on an existing
field.
"""

import frappe


def execute() -> None:
    if not frappe.db.exists("DocType", "Item Group"):
        return
    from ecommerce_integrations.shopware6.custom_fields import setup_custom_fields

    setup_custom_fields()
