"""Install the two Sync-level toggles that drive per-item Sales-Channel
visibility:

* ``sync_visibilities`` (Check, default 0): opt-in per Sync to push
  ``visibilities`` (Shopware) / ``sales_channels`` (Medusa) per
  item instead of broadcasting ``target_sales_channels`` to every
  in-scope item. Keeps the previous behaviour the default so
  existing installs aren't surprised.
* ``default_sales_channel`` (Data): channel id the canonical's
  fallback emits for items the resolver doesn't place and that
  aren't members of any ``linked_smart_collections``. Enables the
  "alle Items in Medusa, nur SC-Member auf der primären Channel,
  Rest auf Default" workflow without requiring the operator to
  wire ``Ecommerce Smart Collection Target`` rows per SC.

Both fields are read by
:func:`product_sync.engine.canonical._canonical_visibilities`;
the resolver-based path stays primary (Catalog Mirror /
per-item override / SC Channel Targets win), and the new fields
only kick in when both the resolver returns empty AND the
operator has explicitly opted in.

Idempotent — skipped if the fields already exist.
"""

from __future__ import annotations

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute() -> None:
    if not frappe.db.exists("DocType", "Ecommerce Product Sync"):
        return

    create_custom_fields({
        "Ecommerce Product Sync": [
            {
                "fieldname": "sync_visibilities",
                "label": "Push per-item Sales-Channel visibility",
                "fieldtype": "Check",
                "default": "0",
                "insert_after": "sync_taxes",
                "description": (
                    "When on, the engine emits Shopware ``visibilities`` "
                    "/ Medusa ``sales_channels`` per item via the "
                    "Catalog-Mirror resolver (Catalog Mirror + Smart "
                    "Collections + per-item Channel Override). When off, "
                    "all in-scope items broadcast to every "
                    "``target_sales_channels`` row (legacy behaviour). "
                    "Flip after configuring the resolver layers so "
                    "items currently visible on a channel the resolver "
                    "doesn't know about don't get pruned."
                ),
            },
            {
                "fieldname": "default_sales_channel",
                "label": "Default Sales Channel (fallback)",
                "fieldtype": "Data",
                "insert_after": "sync_visibilities",
                "depends_on": "sync_visibilities",
                "description": (
                    "Channel id used as fallback for items the resolver "
                    "doesn't place and that aren't members of any "
                    "``linked_smart_collections``. Lets you push the "
                    "full catalogue to the backend while reserving the "
                    "primary ``target_sales_channels`` for "
                    "Smart-Collection-member items. Leave empty to "
                    "give such items no channel."
                ),
            },
        ],
    }, ignore_validate=True)
    frappe.db.commit()  # noqa: SLF001
