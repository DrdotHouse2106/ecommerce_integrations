"""Fold ``Item.shopware_description_override`` back into the standard
``Item.description`` field, then drop the custom field.

Course-correction: the override field was meant to give operators a
Shopware-specific description without touching the standard one, but
it just produced two description fields on every Item form (import
would write one, an operator might edit the other, and it was unclear
which one actually reached Shopware). The operator wants exactly one:
the standard ``description`` field, used both ways — imported into
directly, and read directly by the push side's default
``description_source`` (no override-priority code needed once there's
only one field to read).

Non-destructive: any Item that has content in the override field gets
it copied into ``description`` (overwriting — the override was always
meant to be the operative value, so it wins) before the field is
removed. Items without an override are untouched.
"""

import frappe


def execute() -> None:
    if not frappe.db.exists("DocType", "Item"):
        return
    if not frappe.db.exists("Custom Field", "Item-shopware_description_override"):
        return

    rows = frappe.db.sql(
        """SELECT name, shopware_description_override
           FROM `tabItem`
           WHERE shopware_description_override IS NOT NULL
             AND shopware_description_override != ''""",
        as_dict=True,
    )
    for row in rows:
        frappe.db.set_value(
            "Item", row.name, "description", row.shopware_description_override,
            update_modified=False,
        )

    frappe.delete_doc("Custom Field", "Item-shopware_description_override", ignore_permissions=True)
    frappe.db.commit()  # noqa: SLF001
