"""Compute the diff between an ERPNext Item Group tree and a live
backend category tree.

The differ is pure (no I/O, no Frappe DB) — it consumes already-walked
trees and the per-IG mapping dict and emits a :class:`TreeDiff` that the
orchestrator translates into adapter calls.

Buckets:

- ``creates``  — IGs without a matching external category (or whose
  recorded ``<backend>_category_id`` is missing from the live tree).
- ``updates``  — IGs whose external category exists but name /
  description / active drifted.
- ``moves``    — IGs whose parent moved in ERPNext, requiring a
  re-parent in the backend.
- ``noops``    — IGs already in sync (still returned for the preview).
- ``orphans``  — backend categories under the root that don't map to
  any IG. Subject to ``orphan_policy``.
- ``mapping_drift`` — IGs with a recorded external_id that the live
  tree did not produce. The orchestrator clears the mapping and creates
  fresh on the live path; the preview surfaces the drift.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import frappe

from ecommerce_integrations.catalog_mirror.constants import (
    ACTION_CREATE,
    ACTION_MOVE,
    ACTION_NOOP,
    ACTION_UPDATE,
    ORPHAN_DELETE,
    ORPHAN_KEEP,
    ORPHAN_REPORT,
)
from ecommerce_integrations.catalog_mirror.engine.adapters.base import (
    LiveCategoryNode,
)
from ecommerce_integrations.catalog_mirror.engine.preview import (
    MappingDriftNode,
    NodePlan,
    OrphanNode,
)
from ecommerce_integrations.catalog_mirror.walker import ErpTreeNode


@dataclass
class TreeDiff:
    """The result of comparing an ERPNext tree against the live backend.

    The orchestrator applies the buckets in this order:

    1. creates (parent-first; new parent_external_id must exist by the
       time the child is created)
    2. updates (any order — name/description/active patches are
       independent)
    3. moves (bottom-up so a re-parented subtree is moved as one unit)
    4. deletes (only if orphan_policy=delete)
    """

    creates: list[NodePlan] = field(default_factory=list)
    updates: list[NodePlan] = field(default_factory=list)
    moves: list[NodePlan] = field(default_factory=list)
    noops: list[NodePlan] = field(default_factory=list)
    orphans: list[OrphanNode] = field(default_factory=list)
    mapping_drift: list[MappingDriftNode] = field(default_factory=list)
    unresolved_external_root: bool = False


def compute_tree_diff(
    erp_tree: ErpTreeNode,
    live_tree: LiveCategoryNode | None,
    mapping: dict[str, str],
    *,
    name_template: str,
    description_template: str = "",
    orphan_policy: str = ORPHAN_KEEP,
) -> TreeDiff:
    """Compute the diff for one Catalog Mirror.

    ``mapping`` is the persisted ``{item_group_name -> external_id}``
    table (per backend). It's the source of truth for "which live
    category belongs to which IG". When an entry exists but the live
    tree doesn't surface that external_id, the IG goes into
    ``mapping_drift`` (and will be re-created on the next apply).
    """
    diff = TreeDiff()

    if live_tree is None:
        diff.unresolved_external_root = True
        # Without a live root every IG is a create — the orchestrator
        # will short-circuit before applying because the root itself
        # needs to be created or pinned first, but the preview still
        # surfaces the would-be plan so the operator can see it.
        for node in erp_tree.iter_descendants():
            if node is erp_tree:
                # The root itself: created at the live root if needed,
                # so we still need to render it as a create.
                diff.creates.append(_make_create(node, None, name_template, description_template))
            else:
                # parent is the previous-walked node; resolve via
                # mapping in case the parent has been adopted.
                parent_ext = _resolve_parent_external_id(
                    erp_tree, node, mapping,
                )
                diff.creates.append(
                    _make_create(node, parent_ext, name_template, description_template),
                )
        return diff

    # Build name lookup of live tree by external_id so we can resolve
    # both per-IG mapping hits and orphan detection in one pass.
    live_by_id: dict[str, LiveCategoryNode] = {}
    for live_node in _iter_live(live_tree):
        live_by_id[live_node.external_id] = live_node

    matched_live_ids: set[str] = set()

    # The root IG always maps to the live root. The orchestrator pins
    # ``external_root_id`` separately; here we just treat the live root
    # as the match for the ERP root regardless of the mapping table.
    matched_live_ids.add(live_tree.external_id)

    # Walk the ERP tree in pre-order. For each non-root node, decide
    # which bucket it falls into.
    _classify_node(
        erp_node=erp_tree,
        live_match=live_tree,
        proposed_parent_external_id=None,
        erp_tree=erp_tree,
        live_by_id=live_by_id,
        matched_live_ids=matched_live_ids,
        mapping=mapping,
        diff=diff,
        name_template=name_template,
        description_template=description_template,
        is_root=True,
    )

    for child in erp_tree.children:
        _walk_classify(
            erp_node=child,
            erp_parent=erp_tree,
            erp_tree=erp_tree,
            live_by_id=live_by_id,
            matched_live_ids=matched_live_ids,
            mapping=mapping,
            diff=diff,
            name_template=name_template,
            description_template=description_template,
        )

    # Anything under the live root that we didn't match is an orphan.
    suggested = _suggested_orphan_action(orphan_policy)
    for live_node in _iter_live(live_tree):
        if live_node.external_id in matched_live_ids:
            continue
        # The root itself is always matched above; iter_live includes it
        # so the guard catches it cleanly.
        diff.orphans.append(
            OrphanNode(
                external_id=live_node.external_id,
                name=live_node.name,
                path=_path_for(live_node, live_by_id),
                product_count=int(live_node.product_count or 0),
                suggested_action=suggested,
            ),
        )

    return diff


def _walk_classify(
    *,
    erp_node: ErpTreeNode,
    erp_parent: ErpTreeNode,
    erp_tree: ErpTreeNode,
    live_by_id: dict[str, LiveCategoryNode],
    matched_live_ids: set[str],
    mapping: dict[str, str],
    diff: TreeDiff,
    name_template: str,
    description_template: str,
) -> None:
    parent_ext = _resolve_parent_external_id(erp_tree, erp_node, mapping)
    live_match = _resolve_live_match(erp_node, mapping, live_by_id, diff, name_template)
    if live_match is not None:
        matched_live_ids.add(live_match.external_id)

    _classify_node(
        erp_node=erp_node,
        live_match=live_match,
        proposed_parent_external_id=parent_ext,
        erp_tree=erp_tree,
        live_by_id=live_by_id,
        matched_live_ids=matched_live_ids,
        mapping=mapping,
        diff=diff,
        name_template=name_template,
        description_template=description_template,
        is_root=False,
    )

    for child in erp_node.children:
        _walk_classify(
            erp_node=child,
            erp_parent=erp_node,
            erp_tree=erp_tree,
            live_by_id=live_by_id,
            matched_live_ids=matched_live_ids,
            mapping=mapping,
            diff=diff,
            name_template=name_template,
            description_template=description_template,
        )


def _classify_node(
    *,
    erp_node: ErpTreeNode,
    live_match: LiveCategoryNode | None,
    proposed_parent_external_id: str | None,
    erp_tree: ErpTreeNode,
    live_by_id: dict[str, LiveCategoryNode],
    matched_live_ids: set[str],
    mapping: dict[str, str],
    diff: TreeDiff,
    name_template: str,
    description_template: str,
    is_root: bool,
) -> None:
    proposed_name = _render_template(name_template, erp_node)
    proposed_description = _render_template(description_template, erp_node)

    if live_match is None:
        if is_root:
            # The root has no live match only when the whole tree is
            # missing — and that's already handled at compute_tree_diff
            # entry. If we reach here the orchestrator's external_root_id
            # was empty; record a create so the live path can pin it.
            diff.creates.append(
                _make_create(
                    erp_node, proposed_parent_external_id,
                    name_template, description_template,
                ),
            )
            return
        diff.creates.append(
            _make_create(
                erp_node, proposed_parent_external_id,
                name_template, description_template,
            ),
        )
        return

    name_changed = (live_match.name or "") != proposed_name
    desc_changed = (live_match.description or "") != (proposed_description or "")

    # For the root we don't propose a parent (the live root keeps its
    # current parent — typically the sales-channel's navigation root or
    # whatever Shopware has). Only non-root nodes can move.
    parent_changed = (
        not is_root
        and (live_match.parent_external_id or None) != proposed_parent_external_id
    )

    plan = NodePlan(
        item_group=erp_node.name,
        item_group_path=_ig_path(erp_tree, erp_node),
        action=ACTION_NOOP,
        proposed_name=proposed_name,
        proposed_parent_external_id=proposed_parent_external_id,
        proposed_description=proposed_description or None,
        proposed_active=True,
        current_external_id=live_match.external_id,
        current_name=live_match.name,
        current_parent_external_id=live_match.parent_external_id,
        current_description=live_match.description,
        current_active=bool(live_match.active),
        children_count=len(erp_node.children),
        product_count=int(live_match.product_count or 0),
    )

    if parent_changed:
        plan.action = ACTION_MOVE
        diff.moves.append(plan)
        # A move can co-exist with name/desc drift; the orchestrator
        # patches both. We classify as ``move`` so the preview surfaces
        # it as the operationally bigger change.
        if name_changed or desc_changed:
            plan.notes.append("also has name/description drift")
        return

    if name_changed or desc_changed:
        plan.action = ACTION_UPDATE
        diff.updates.append(plan)
        return

    diff.noops.append(plan)


def _resolve_live_match(
    erp_node: ErpTreeNode,
    mapping: dict[str, str],
    live_by_id: dict[str, LiveCategoryNode],
    diff: TreeDiff,
    name_template: str,
) -> LiveCategoryNode | None:
    """Find the live category for ``erp_node``.

    Resolution order:

    1. ``mapping[erp_node.name]`` if set — the persisted per-IG link.
       When the recorded id isn't in the live tree, record drift and
       return None so the node falls into ``creates``.
    2. No name-based fallback. Name matching is the Adopt workflow's
       job (operator-driven via :func:`api.adopt_node`); the differ
       must not silently re-attach a same-named live category, because
       that would re-link the wrong one if the operator just renamed
       an IG.
    """
    ext_id = mapping.get(erp_node.name)
    if not ext_id:
        return None
    live = live_by_id.get(ext_id)
    if live is None:
        diff.mapping_drift.append(
            MappingDriftNode(
                item_group=erp_node.name,
                stale_external_id=ext_id,
                proposed_name=_render_template(name_template, erp_node),
            ),
        )
        return None
    return live


def _resolve_parent_external_id(
    erp_tree: ErpTreeNode,
    erp_node: ErpTreeNode,
    mapping: dict[str, str],
) -> str | None:
    """Walk up to find the parent IG's external_id (or None for root)."""
    if erp_node is erp_tree:
        return None
    parent_path = _find_parent(erp_tree, erp_node.name)
    if parent_path is None:
        return None
    # If the parent is the ERP root, the parent_external_id is the
    # mirror's external_root_id — resolved by the orchestrator before
    # calling the differ via ``mapping[<root_name>]``.
    return mapping.get(parent_path.name)


def _find_parent(
    tree: ErpTreeNode, target_name: str,
) -> ErpTreeNode | None:
    for node in tree.iter_descendants():
        if any(c.name == target_name for c in node.children):
            return node
    return None


def _make_create(
    erp_node: ErpTreeNode,
    parent_external_id: str | None,
    name_template: str,
    description_template: str,
) -> NodePlan:
    return NodePlan(
        item_group=erp_node.name,
        item_group_path=erp_node.name,
        action=ACTION_CREATE,
        proposed_name=_render_template(name_template, erp_node),
        proposed_parent_external_id=parent_external_id,
        proposed_description=_render_template(description_template, erp_node) or None,
        proposed_active=True,
        children_count=len(erp_node.children),
    )


def _ig_path(tree: ErpTreeNode, node: ErpTreeNode) -> str:
    """Return ``Root / Parent / Node`` style path for the preview."""
    chain: list[str] = []
    current: ErpTreeNode | None = node
    while current is not None:
        chain.append(current.item_group_name or current.name)
        if current is tree:
            break
        current = _find_parent(tree, current.name)
    return " / ".join(reversed(chain))


def _iter_live(root: LiveCategoryNode):
    yield root
    for child in root.children:
        yield from _iter_live(child)


def _path_for(
    node: LiveCategoryNode, live_by_id: dict[str, LiveCategoryNode],
) -> str:
    chain: list[str] = []
    current: LiveCategoryNode | None = node
    guard = 0
    while current is not None and guard < 64:
        chain.append(current.name or current.external_id)
        if not current.parent_external_id:
            break
        current = live_by_id.get(current.parent_external_id)
        guard += 1
    return " / ".join(reversed(chain))


def _suggested_orphan_action(orphan_policy: str) -> str:
    if orphan_policy == ORPHAN_DELETE:
        return "delete"
    if orphan_policy == ORPHAN_REPORT:
        return "report"
    return ORPHAN_KEEP


def _render_template(template: str, erp_node: ErpTreeNode) -> str:
    """Render the name/description template over the IG.

    Empty templates short-circuit to the empty string so the caller can
    treat a blank ``description_template`` as "no description". The
    Frappe render path is only exercised when the template contains a
    template tag — plain pass-through values like
    ``{{ item_group.item_group_name }}`` are common enough that the fast
    path matters on big trees.
    """
    if not template:
        return ""
    if "{{" not in template and "{%" not in template:
        return template
    try:
        return frappe.render_template(
            template,
            {
                "item_group": {
                    "name": erp_node.name,
                    "item_group_name": erp_node.item_group_name,
                    "parent_item_group": erp_node.parent_item_group,
                    "is_group": erp_node.is_group,
                    "has_items": erp_node.has_items,
                },
            },
        )
    except Exception:
        # A broken template shouldn't crash the differ — surface the
        # raw IG name and let the preview's per-row notes carry the
        # warning if the orchestrator chooses to.
        return erp_node.item_group_name or erp_node.name
