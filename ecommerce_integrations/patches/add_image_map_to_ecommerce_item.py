"""Install ``Ecommerce Item.pushed_image_map`` custom field.

The product-sync image pipeline pushes ERPNext file URLs to
Shopware, which fetches them and re-hosts the binaries under its own
CDN. Stock ERPNext has no way to remember "I already pushed this
URL and got back this media-UUID", so every subsequent preview would
flag the image as drift forever (the proposed ERP URL never matches
the live CDN URL).

This field stores a JSON map ``{erp_basename: shopware_media_uuid}``
that the differ consults: if every proposed image's basename has a
known mapping AND the live product carries that media-UUID, no diff
is emitted. Otherwise the diff fires and the apply step uploads +
re-maps.

Idempotent — skipped if the field is already there.
"""

from __future__ import annotations

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute() -> None:
    if not frappe.db.exists("DocType", "Ecommerce Item"):
        return
    create_custom_fields({
        "Ecommerce Item": [
            {
                "fieldname": "pushed_image_map",
                "label": "Pushed Image Map (JSON)",
                "fieldtype": "Long Text",
                "insert_after": "item_synced_on",
                "read_only": 1,
                "hidden": 1,
                "description": (
                    "Per-Item map of ERP image basename → backend media "
                    "UUID, written by the apply step after successful "
                    "image upload. The differ reads it to suppress "
                    "spurious image-drift diffs when the live product "
                    "already carries the mapped media."
                ),
            },
        ],
    }, ignore_validate=True)
    frappe.db.commit()  # noqa: SLF001
