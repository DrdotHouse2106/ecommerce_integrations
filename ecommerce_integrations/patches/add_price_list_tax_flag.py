"""Install ``Price List.custom_price_includes_tax`` custom field.

ERPNext's stock ``Price List`` doctype doesn't carry a gross/net flag,
which forces this connector to use a single global ``Shopware Setting.
default_price_list_includes_tax`` for the whole catalogue. Real
installs typically run several Price Lists in parallel with different
tax-modes (one net B2B list, one gross B2C list, MSRP, …). A per-list
flag is what the canonical pricing builder needs to compute gross
correctly without operator-side hacks.

The custom field is idempotent (skipped if already installed). The
default is ``0`` (net) which matches the ERPNext-side convention.
"""

from __future__ import annotations

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute() -> None:
    if not frappe.db.exists("DocType", "Price List"):
        return
    create_custom_fields({
        "Price List": [
            {
                "fieldname": "custom_price_includes_tax",
                "label": "Prices include tax (gross)",
                "fieldtype": "Check",
                "insert_after": "currency",
                "default": "0",
                "description": (
                    "When enabled, the prices in this list already "
                    "include tax (gross). Used by ecommerce_integrations "
                    "to derive net/gross correctly when pushing prices "
                    "to backends like Shopware or Medusa."
                ),
            },
        ],
    }, ignore_validate=True)
    frappe.db.commit()  # noqa: SLF001
