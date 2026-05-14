"""Install the cross-backend ``ecommerce_channel_overrides`` table on ``Item``.

The custom field carries per-item override rows of the
``Ecommerce Channel Override`` child doctype. Each row pins a single
sales channel on a single backend (Shopware or Medusa) to either
``include`` (with a visibility) or ``exclude``.

The resolver in :mod:`ecommerce_integrations.catalog_mirror.api`
gives these rows the highest priority — they override both Catalog
Mirror node assignments and Smart Collection matches.

Idempotent: :func:`create_custom_fields` upserts by ``fieldname`` so
a second run is a no-op.

No-op safe: when the ``Item`` doctype isn't present on this site
(vanilla Frappe install without ERPNext) the patch returns early.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute() -> None:
    if not frappe.db.exists("DocType", "Item Group"):
        # Site doesn't have ERPNext installed — Item doctype won't
        # exist either, so the custom field has nothing to attach to.
        return
    if not frappe.db.exists("DocType", "Item"):
        return

    insert_after = "item_group"
    if frappe.db.exists(
        "Custom Field", {"dt": "Item", "fieldname": "shopware_channels_section"}
    ):
        insert_after = "shopware_channels_section"

    create_custom_fields(
        {
            "Item": [
                {
                    "fieldname": "ecommerce_channel_overrides",
                    "label": "Channel Visibility Overrides (cross-backend)",
                    "fieldtype": "Table",
                    "options": "Ecommerce Channel Override",
                    "insert_after": insert_after,
                    "description": (
                        "Per-item overrides for which Shopware/Medusa sales channels "
                        "this item appears in. Highest priority — wins over Catalog "
                        "Mirror and Smart Collections."
                    ),
                },
            ],
        },
        ignore_validate=True,
    )
