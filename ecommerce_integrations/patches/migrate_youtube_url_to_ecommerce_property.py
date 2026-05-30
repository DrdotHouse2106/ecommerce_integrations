"""Move ``Item.youtube_video_url`` values into
``Item.ecommerce_properties`` rows (``property_type = "Custom Field"``)
so they flow through the universal customField routing path.

The YouTube URL used to be a dedicated Item custom field that the
product-sync engine special-cased into the Shopware
``customFields.erpnext_youtube_video_url`` slot via a hardcoded
mapping in ``payload.py``. That worked but kept "YouTube URL" as
the only customField hardcoded by name, while every other channel
flag (Idealo, Google Shopping, age rating, …) flows neutrally
through either:

* operator-configured rows on
  ``Shopware Setting.item_custom_field_mappings``, or
* ``Item Ecommerce Property`` rows with
  ``property_type = "Custom Field"`` (Source-2 in
  ``_canonical_dynamic_custom_fields``).

Migrating the YouTube URL into Source-2 removes the last hardcoded
branch — the engine now treats every customField uniformly. The
derived Shopware key is preserved (``shopware_custom_field_name``
of "YouTube Video URL" is ``erpnext_youtube_video_url``), so the
storefront iframe / preview block keeps reading from the same slot
without any storefront-side change.

The patch is idempotent — re-running on an item that already has
the migrated property row updates the value in place rather than
duplicating. The legacy ``Item.youtube_video_url`` custom field is
left in place so historical data isn't lost; subsequent edits
should happen on the ecommerce_properties row.
"""

from __future__ import annotations

import frappe

PROPERTY_NAME = "YouTube Video URL"


def execute() -> None:
    if not frappe.db.exists("DocType", "Item Ecommerce Property"):
        return
    meta = frappe.get_meta("Item")
    present = {f.fieldname for f in meta.get("fields") or []}
    if "youtube_video_url" not in present:
        return

    rows = frappe.db.sql(
        """SELECT name, youtube_video_url FROM `tabItem`
           WHERE youtube_video_url IS NOT NULL AND youtube_video_url != ''""",
        as_dict=True,
    )
    migrated, skipped = 0, 0
    for r in rows:
        item_code = r["name"]
        url = (r["youtube_video_url"] or "").strip()
        if not url:
            continue
        existing = frappe.db.get_value(
            "Item Ecommerce Property",
            {
                "parent": item_code,
                "parenttype": "Item",
                "property_name": PROPERTY_NAME,
            },
            ["name", "property_value"],
            as_dict=True,
        )
        if existing:
            if (existing.get("property_value") or "").strip() == url:
                skipped += 1
                continue
            frappe.db.set_value(
                "Item Ecommerce Property", existing["name"],
                {
                    "property_value": url,
                    "property_type": "Custom Field",
                    "sync_to_shopware": 1,
                },
                update_modified=False,
            )
            migrated += 1
        else:
            iep = frappe.get_doc({
                "doctype": "Item Ecommerce Property",
                "parent": item_code,
                "parenttype": "Item",
                "parentfield": "ecommerce_properties",
                "property_name": PROPERTY_NAME,
                "property_value": url,
                "property_type": "Custom Field",
                "sync_to_shopware": 1,
                "sync_to_medusa": 0,
                "filterable": 0,
            })
            iep.insert(ignore_permissions=True)
            migrated += 1
    frappe.db.commit()  # noqa: SLF001
    frappe.logger("ecommerce_integrations").info(
        f"YouTube URL migration: {migrated} migrated, {skipped} already up-to-date "
        f"(out of {len(rows)} items with youtube_video_url set).",
    )
