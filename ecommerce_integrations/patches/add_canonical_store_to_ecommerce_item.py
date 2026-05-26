"""Install ``Ecommerce Item.last_synced_canonical`` custom field.

True per-field delta sync needs the previous canonical payload, not
just its hash, so the differ can compute *which* fields changed and
the apply step can push a partial Shopware product entity (the
``_action/sync`` endpoint treats missing keys as "leave alone").

Without this field a single-attribute change in the canonical
schema — adding ``delivery_time`` for example — invalidates every
stored hash and forces a full-payload re-push for every item, even
though the actual data is byte-identical except for the new field.

Storage cost: ~5-10 kB per item (gzip-base64 encoded JSON). For a
37k-item catalogue that's ~200-300 MB of MariaDB longtext — within
budget for production sites and recoverable from a fresh sync if
ever truncated.

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
                "fieldname": "last_synced_canonical",
                "label": "Last Synced Canonical (gzip+b64)",
                "fieldtype": "Long Text",
                "insert_after": "pushed_image_map",
                "read_only": 1,
                "hidden": 1,
                "description": (
                    "gzip-then-base64 encoded JSON snapshot of the "
                    "canonical payload that was pushed on the last "
                    "successful apply. The differ compares this "
                    "against the freshly built canonical to compute "
                    "per-field changes so the apply step can push a "
                    "partial Shopware payload instead of re-sending "
                    "every field on every drift."
                ),
            },
        ],
    }, ignore_validate=True)
    frappe.db.commit()  # noqa: SLF001
