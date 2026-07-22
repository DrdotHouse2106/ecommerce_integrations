"""Re-sync all Shopware custom fields onto existing sites.

Originally written just for ``shopware_description_override`` on Item,
but since it re-runs the *entire* ``setup_custom_fields()`` (not a
narrow per-field dict), it also picks up every other field that had
been added to ``shopware6/custom_fields.py`` since this site's install
without a dedicated patch — notably ``Item.delivery_time``,
``Item.seo_title`` / ``seo_meta_description`` / ``seo_keywords``,
``Item Group.seo_title`` / ``seo_meta_description`` / ``seo_keywords``,
and the ``Sales Order`` block including ``shopware_order_id`` (whose
absence broke ``handle_transaction_state_change`` and
``sync_order_from_webhook`` with ``Unknown column 'shopware_order_id'``
on sites that installed the app before that block existed).

The field definitions live in ``shopware6/custom_fields.py``, but that
dict is only applied on fresh installs (``after_install``) and by the
one-time ``update_shopware_custom_fields`` patch — already consumed on
existing sites, so later additions to the dict never reached them.

Idempotent: ``create_custom_fields`` (called inside
``setup_custom_fields``) upserts by fieldname.
"""

import frappe


def execute() -> None:
    if not frappe.db.exists("DocType", "Item"):
        return
    from ecommerce_integrations.shopware6.custom_fields import setup_custom_fields

    setup_custom_fields()
