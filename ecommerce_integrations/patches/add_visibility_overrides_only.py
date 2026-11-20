"""Install ``Ecommerce Product Sync.visibility_overrides_only``.

Strict visibility mode for shops that keep the per-item channel list
in ERPNext (``Item.ecommerce_channel_overrides`` include rows): an item
is visible exactly on the channels listed for it and nowhere else.
Without include rows it is hidden on every channel — no fallback to the
default channel or to ``target_sales_channels``, neither on update nor
when the product is first created.

Idempotent — skipped if the field already exists.
"""

from __future__ import annotations

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute() -> None:
    if not frappe.db.exists("DocType", "Ecommerce Product Sync"):
        return
    create_custom_fields({
        "Ecommerce Product Sync": [{
            "fieldname": "visibility_overrides_only",
            "label": "Only channels from per-item overrides",
            "fieldtype": "Check",
            "default": "0",
            "insert_after": "default_sales_channel",
            "depends_on": "sync_visibilities",
            "description": (
                "Items are visible only on the channels listed in their "
                "Channel Overrides (include rows) plus Catalog Mirror / "
                "Smart Collection placements. Items without any placement "
                "are hidden on every channel, also when first created. "
                "Ignores the default channel fallback."
            ),
        }],
    }, ignore_validate=True)
    frappe.db.commit()  # noqa: SLF001
