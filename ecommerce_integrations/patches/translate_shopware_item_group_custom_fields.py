"""Re-sync Shopware custom fields to pick up German label/description
translations on already-migrated sites.

``Item Group.shopware_section`` / ``shopware_active`` / ``shopware_priority``
/ ``category_image`` shipped with English labels and descriptions from
the very first version of ``shopware6/custom_fields.py`` — overlooked
in the later full-plugin German translation pass, which only reached
fields added after it (the neighbouring ``seo_title`` /
``seo_meta_description`` / ``seo_keywords`` fields were already
German).

``setup_custom_fields()`` is only applied on fresh installs
(``after_install``); on an existing site with the previous English
values already saved to ``tabCustom Field``, only a fresh
``create_custom_fields(..., update=True)`` run overwrites them —
which is exactly what the earlier ``add_shopware_description_override_field``
patch already does, except that patch is patch-log-recorded as done on
this site and won't re-run for a text-only change made after it.

Idempotent: ``create_custom_fields`` (called inside
``setup_custom_fields``) upserts by fieldname.
"""

import frappe


def execute() -> None:
    if not frappe.db.exists("DocType", "Item Group"):
        return
    from ecommerce_integrations.shopware6.custom_fields import setup_custom_fields

    setup_custom_fields()
