"""One-time backfill: create ERPNext Item Groups from an existing
Shopware category tree.

For shops that maintained their category tree in Shopware before
adopting this integration (no matching Item Group tree exists yet in
ERPNext). Catalog Mirror's own "Adopt" flow assumes the Item Group
already exists and just needs its ``shopware_category_id`` pinned —
it does not create Item Groups from scratch. This module does that
part, once, then hands off to Catalog Mirror for ongoing sync.

Reuses ``ShopwareCatalogAdapter.fetch_tree`` (the same tree-walk
Catalog Mirror itself uses for previews) instead of re-implementing
Shopware category pagination. Every Shopware category with no parent
(``parentId is None``) is treated as a root — Shopware installations
can have more than one such tree (main navigation, footer navigation,
...); importing every root's subtree is what "all categories" means
here.

Matching is by ``shopware_category_id`` first (idempotent re-run: a
node already linked to an Item Group is updated in place, not
duplicated), then by name (an existing hand-made Item Group with a
matching name and no ``shopware_category_id`` yet is *adopted* — same
semantics as Catalog Mirror's manual Adopt, just automatic). A name
already claimed by a *different* mapped category is disambiguated
with a suffix rather than silently dropped or misfiled.
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe import _

from ecommerce_integrations.catalog_mirror.engine.adapters.base import LiveCategoryNode
from ecommerce_integrations.catalog_mirror.engine.adapters.shopware import ShopwareCatalogAdapter
from ecommerce_integrations.shopware6.connection import temp_shopware_session
from ecommerce_integrations.shopware6.constants import SETTING_DOCTYPE


@frappe.whitelist()
def import_categories_from_shopware() -> dict[str, Any]:
    """Entry point for the "Import Categories from Shopware" button."""
    frappe.only_for("System Manager")

    setting = frappe.get_doc(SETTING_DOCTYPE)
    if not setting.is_enabled():
        frappe.throw(_("Bitte zuerst die Shopware-Integration aktivieren"))

    stats: dict[str, Any] = {
        "created": 0,
        "adopted": 0,
        "updated": 0,
        "skipped_ignored": 0,
        "images_set": 0,
        "errors": [],
    }

    root_parent = _ensure_root_item_group(setting.category_sync_root or "Products")

    root_ids = _fetch_absolute_root_ids()
    if not root_ids:
        stats["errors"].append("No categories found in Shopware.")
        return stats

    adapter = ShopwareCatalogAdapter()
    for root_id in root_ids:
        tree_root = adapter.fetch_tree(root_id)
        if not tree_root:
            stats["errors"].append(f"Could not fetch category tree for root {root_id}.")
            continue
        # The technical root itself isn't imported as an Item Group —
        # only its children are, parented under category_sync_root.
        # Mirrors the export side's "skip_root_category" convention.
        for child in tree_root.children:
            _import_node(child, parent_item_group=root_parent, stats=stats)

    frappe.db.commit()
    return stats


def _ensure_root_item_group(name: str) -> str:
    if frappe.db.exists("Item Group", name):
        return name

    true_root = frappe.db.get_value(
        "Item Group", {"is_group": 1, "parent_item_group": ["in", ["", None]]}, "name"
    )
    ig = frappe.new_doc("Item Group")
    ig.item_group_name = name
    ig.is_group = 1
    ig.parent_item_group = true_root
    ig.insert(ignore_permissions=True)
    return ig.name


def _import_node(node: LiveCategoryNode, parent_item_group: str, stats: dict[str, Any]) -> None:
    if node.ignored:
        stats["skipped_ignored"] += 1
        return

    item_group_name = _resolve_item_group(node, parent_item_group, stats)
    if item_group_name is None:
        return

    for child in node.children:
        _import_node(child, parent_item_group=item_group_name, stats=stats)


def _resolve_item_group(node: LiveCategoryNode, parent_item_group: str, stats: dict[str, Any]) -> str | None:
    try:
        # 1. Already imported on a previous run of this script.
        linked_name = frappe.db.get_value(
            "Item Group", {"shopware_category_id": node.external_id}, "name"
        )
        if linked_name:
            ig = frappe.get_doc("Item Group", linked_name)
            _apply_fields(ig, node, parent_item_group)
            ig.save(ignore_permissions=True)
            stats["updated"] += 1
            _maybe_set_image(ig, node, stats)
            return ig.name

        target_name = node.name or _("Unnamed Category")
        existing = frappe.db.get_value(
            "Item Group",
            {"item_group_name": target_name},
            ["name", "shopware_category_id"],
            as_dict=True,
        )

        if existing and not existing.shopware_category_id:
            # 2. Hand-made Item Group with a matching name — adopt it.
            ig = frappe.get_doc("Item Group", existing.name)
            ig.shopware_category_id = node.external_id
            _apply_fields(ig, node, parent_item_group)
            ig.save(ignore_permissions=True)
            stats["adopted"] += 1
            _maybe_set_image(ig, node, stats)
            return ig.name

        if existing and existing.shopware_category_id and existing.shopware_category_id != node.external_id:
            # 3. Name already claimed by a *different* mapped category —
            # disambiguate instead of silently dropping this one.
            target_name = f"{target_name} ({node.external_id[:8]})"

        # 4. New Item Group.
        ig = frappe.new_doc("Item Group")
        ig.item_group_name = target_name
        ig.shopware_category_id = node.external_id
        _apply_fields(ig, node, parent_item_group)
        ig.insert(ignore_permissions=True)
        stats["created"] += 1
        _maybe_set_image(ig, node, stats)
        return ig.name

    except Exception as e:
        stats["errors"].append(f"{node.name} ({node.external_id}): {e}")
        frappe.log_error(title="Shopware category import failed", message=frappe.get_traceback())
        return None


def _apply_fields(ig, node: LiveCategoryNode, parent_item_group: str) -> None:
    ig.parent_item_group = parent_item_group
    ig.is_group = 1 if node.children else 0
    if node.description:
        ig.description = node.description
    ig.shopware_active = 1 if node.active else 0

    meta = frappe.get_meta("Item Group")
    if node.meta_title and meta.has_field("seo_title"):
        ig.seo_title = node.meta_title
    if node.meta_description and meta.has_field("seo_meta_description"):
        ig.seo_meta_description = node.meta_description


def _maybe_set_image(ig, node: LiveCategoryNode, stats: dict[str, Any]) -> None:
    try:
        url = _fetch_category_image_url(node.external_id)
    except Exception:
        return
    if url and ig.category_image != url:
        frappe.db.set_value("Item Group", ig.name, "category_image", url)
        stats["images_set"] += 1


@temp_shopware_session
def _fetch_absolute_root_ids(client) -> list[str]:
    response = client.request_post(
        "search/category",
        {"filter": [{"type": "equals", "field": "parentId", "value": None}], "limit": 500},
    )
    return [c["id"] for c in (response.data or []) if c.get("id")]


@temp_shopware_session
def _fetch_category_image_url(client, category_id: str) -> str | None:
    response = client.request_get(f"category/{category_id}?associations[media]=")
    data = response.data or {}
    media = data.get("media") or {}
    return media.get("url")
