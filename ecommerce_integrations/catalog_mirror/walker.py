"""Walk the ERPNext Item Group tree under a root, producing a typed tree.

The walker is the only piece of the Catalog Mirror pipeline that talks
to ``tabItem Group`` directly. Everything downstream (differ, adapters,
orchestrator) consumes :class:`ErpTreeNode`.

Performance: a single grouped ``COUNT(*) FROM tabItem GROUP BY
item_group`` query populates ``has_items`` for every leaf in one
round-trip — per-IG lookups would be N+1 on stores with thousands of
groups.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import frappe


@dataclass
class ErpTreeNode:
    """One node in the ERPNext Item Group tree.

    Attributes mirror ``tabItem Group`` 1:1 plus the computed
    ``has_items`` flag (any non-disabled ``tabItem`` rows where
    ``item_group == self.name``). ``children`` is populated in nested-set
    order (``lft`` ascending) so the walk yields a deterministic tree
    shape — important for the differ's parent-before-child apply order.
    """

    name: str
    item_group_name: str
    parent_item_group: str | None
    is_group: int
    has_items: bool = False
    catalog_mirror_skip: int = 0
    # Content fields mirrored into the backend category (and folded into
    # the canonical hash). Loaded straight from ``tabItem Group``.
    description: str | None = None
    seo_title: str | None = None
    seo_meta_description: str | None = None
    children: list[ErpTreeNode] = field(default_factory=list)

    def iter_descendants(self):
        """Pre-order traversal yielding every node in this subtree."""
        yield self
        for child in self.children:
            yield from child.iter_descendants()


def walk_erpnext_tree(
    root_ig_name: str,
    *,
    include_inactive_leaves: bool = False,
) -> ErpTreeNode:
    """Return the full Item Group subtree rooted at ``root_ig_name``.

    ``include_inactive_leaves=False`` (default) prunes leaf IGs whose
    ``is_group=0`` AND have zero items — most stores keep historical
    "Misc"-style groups around that should not be mirrored. Setting
    ``True`` mirrors every leaf regardless.

    Item Groups with the custom field ``catalog_mirror_skip=1`` (added
    by ``patches/setup_catalog_mirror``) are excluded *along with their
    entire subtree* — the typical use case is a Sale/Promo branch that
    Smart Collections owns and Catalog Mirror should ignore.
    """
    if not frappe.db.exists("Item Group", root_ig_name):
        raise ValueError(f"Item Group {root_ig_name!r} does not exist")

    # Single grouped query for has_items — keyed by item_group, with the
    # disabled filter applied so soft-deleted items don't keep ghost
    # leaves in the mirror. Loaded once and shared across the recursive
    # build below.
    item_counts = _fetch_item_counts()

    return _build_node(root_ig_name, item_counts, include_inactive_leaves)


def _fetch_item_counts() -> dict[str, int]:
    """Return ``{item_group_name: count_of_non_disabled_items}``."""
    rows = frappe.db.sql(
        """
        SELECT item_group, COUNT(*) AS c
        FROM `tabItem`
        WHERE COALESCE(disabled, 0) = 0
        GROUP BY item_group
        """,
        as_dict=True,
    )
    return {r.item_group: int(r.c) for r in rows if r.item_group}


def _build_node(
    name: str,
    item_counts: dict[str, int],
    include_inactive_leaves: bool,
) -> ErpTreeNode:
    """Recursively materialise an :class:`ErpTreeNode` for ``name``."""
    ig = frappe.db.get_value(
        "Item Group",
        name,
        [
            "name",
            "item_group_name",
            "parent_item_group",
            "is_group",
            "catalog_mirror_skip",
            "description",
            "seo_title",
            "seo_meta_description",
        ],
        as_dict=True,
    )
    if ig is None:
        raise ValueError(f"Item Group {name!r} disappeared mid-walk")

    node = ErpTreeNode(
        name=ig.name,
        item_group_name=ig.item_group_name or ig.name,
        parent_item_group=ig.parent_item_group,
        is_group=int(ig.is_group or 0),
        has_items=item_counts.get(ig.name, 0) > 0,
        catalog_mirror_skip=int(ig.catalog_mirror_skip or 0),
        description=ig.description,
        seo_title=ig.seo_title,
        seo_meta_description=ig.seo_meta_description,
    )

    # Children in nested-set order so the apply phase walks the same
    # deterministic shape every time. Skipped subtrees are dropped
    # entirely — descendants of a skipped IG never appear in the tree.
    children = frappe.get_all(
        "Item Group",
        filters={"parent_item_group": ig.name},
        fields=["name"],
        order_by="lft asc",
    )
    for child in children:
        child_node = _build_node(child.name, item_counts, include_inactive_leaves)
        if child_node.catalog_mirror_skip:
            continue
        # Prune zero-item leaves unless the operator opted in.
        if (
            not include_inactive_leaves
            and not child_node.is_group
            and not child_node.has_items
        ):
            continue
        node.children.append(child_node)

    return node
