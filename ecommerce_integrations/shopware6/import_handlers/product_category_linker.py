"""One-time backfill: link ERPNext Items to the Shopware categories
they actually belong to.

Companion to ``category_importer`` — that module only creates the
Item Group *tree*; this one assigns individual Items to those groups
based on Shopware's real product-to-category relationships
(``Product.categoryIds``).

Deliberately non-destructive: never touches an Item's existing
``item_group`` (e.g. one carried over from a WeClapp import) — every
Shopware category an Item belongs to is added as an
``additional_item_groups`` row instead (see
``patches.add_item_additional_groups_field``), on top of whatever
primary Item Group it already has. A Shopware category only gets
linked if it was already imported as an Item Group with a matching
``shopware_category_id`` — run "Kategorien aus Shopware importieren"
(category_importer) first.
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe import _

from ecommerce_integrations.shopware6.connection import get_shopware_client
from ecommerce_integrations.shopware6.constants import MODULE_NAME, SETTING_DOCTYPE

_PAGE_SIZE = 250


@frappe.whitelist()
def link_item_categories_from_shopware() -> dict[str, Any]:
    """Entry point for the "Artikel-Kategorien aus Shopware verknüpfen" button."""
    frappe.only_for("System Manager")

    setting = frappe.get_doc(SETTING_DOCTYPE)
    if not setting.is_enabled():
        frappe.throw(_("Bitte zuerst die Shopware-Integration aktivieren"))

    stats: dict[str, Any] = {
        "products_scanned": 0,
        "items_updated": 0,
        "rows_added": 0,
        "errors": [],
    }

    category_to_item_group = _build_category_item_group_map()
    if not category_to_item_group:
        stats["errors"].append(
            _('Keine importierten Kategorien gefunden — zuerst "Kategorien aus Shopware importieren" ausführen.')
        )
        return stats

    product_to_item = _build_product_item_map()
    if not product_to_item:
        stats["errors"].append(_("Keine mit Shopware synchronisierten Artikel gefunden."))
        return stats

    client = get_shopware_client()
    page = 1
    while True:
        response = client.request_post(
            "search/product",
            {
                "limit": _PAGE_SIZE,
                "page": page,
                "includes": {"product": ["id", "categoryIds"]},
            },
        )
        products = response.data or []
        if not products:
            break

        for product in products:
            stats["products_scanned"] += 1
            item_code = product_to_item.get(product.get("id"))
            if not item_code:
                continue

            category_ids = product.get("categoryIds") or []
            item_groups = {
                category_to_item_group[c] for c in category_ids if c in category_to_item_group
            }
            if not item_groups:
                continue

            added = _add_additional_item_groups(item_code, item_groups)
            if added:
                stats["items_updated"] += 1
                stats["rows_added"] += added

        frappe.db.commit()
        page += 1

    return stats


def _build_category_item_group_map() -> dict[str, str]:
    rows = frappe.get_all(
        "Item Group",
        filters={"shopware_category_id": ["is", "set"]},
        fields=["name", "shopware_category_id"],
    )
    return {row.shopware_category_id: row.name for row in rows if row.shopware_category_id}


def _build_product_item_map() -> dict[str, str]:
    rows = frappe.get_all(
        "Ecommerce Item",
        filters={"integration": MODULE_NAME},
        fields=["erpnext_item_code", "integration_item_code"],
    )
    return {row.integration_item_code: row.erpnext_item_code for row in rows if row.integration_item_code}


def _add_additional_item_groups(item_code: str, item_groups: set[str]) -> int:
    """Append any of ``item_groups`` not already on the Item (as its
    primary item_group or an existing additional_item_groups row).

    Returns the number of rows added.
    """
    try:
        item = frappe.get_doc("Item", item_code)
    except frappe.DoesNotExistError:
        return 0

    existing = {item.item_group} | {row.item_group for row in (item.get("additional_item_groups") or [])}
    to_add = item_groups - existing
    if not to_add:
        return 0

    for ig in to_add:
        item.append("additional_item_groups", {"item_group": ig})

    try:
        item.save(ignore_permissions=True)
    except Exception:
        frappe.log_error(
            title="Shopware item-category linking failed",
            message=frappe.get_traceback(),
        )
        return 0

    return len(to_add)
