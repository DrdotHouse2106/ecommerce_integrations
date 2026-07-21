"""Install the ``additional_item_groups`` Table MultiSelect on Item.

ERPNext's native ``Item.item_group`` is single-valued — an Item sits in
exactly one Item Group. Shopware (and most storefronts) let a product
live in several categories at once. Previously the only way to get
multi-category assignment out of this app was the separate Webshop
app's ``Website Item`` / ``Website Item Group`` tables (see
``get_all_item_categories`` in ``shopware6/export/category_handler.py``).

This field gives operators multi-category assignment without requiring
Webshop: each row links to an Item Group whose mapped Shopware category
the product should *additionally* be placed in, on top of its native
``item_group``.

Idempotent: ``create_custom_fields`` upserts by ``fieldname``, and
``reload_doc`` is safe to call on every migrate.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

ADDITIONAL_ITEM_GROUPS_FIELD = {
    "Item": [
        {
            "fieldname": "additional_item_groups",
            "label": "Additional Categories (Shopware)",
            "fieldtype": "Table MultiSelect",
            "options": "Item Additional Group",
            "insert_after": "item_group",
            "description": (
                "Extra Shopware categories for this Item, on top of its "
                "main Item Group above. Use this instead of / alongside "
                "the Webshop app's Website Item Groups."
            ),
        },
    ],
}


def execute() -> None:
    if not frappe.db.exists("DocType", "Item"):
        return
    frappe.reload_doc("shopware6", "doctype", "item_additional_group")
    create_custom_fields(ADDITIONAL_ITEM_GROUPS_FIELD, ignore_validate=True)
