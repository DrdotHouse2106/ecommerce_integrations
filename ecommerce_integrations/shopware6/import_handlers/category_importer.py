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

Matching is by ``shopware_category_id`` only (idempotent re-run: a
node already linked to an Item Group is updated in place, not
duplicated). Deliberately does **not** auto-adopt a same-named
pre-existing Item Group the way Catalog Mirror's manual per-node
Adopt does — a name match against a hand-maintained (e.g. WeClapp)
tree is a coincidence, not proof the two nodes should share identity,
and blindly reparenting/rewriting the existing node under it turned
out to rearrange large parts of an operator's real tree in one run.
Any name collision — with an unlinked hand-made group or with a
*different* already-mapped category — is disambiguated with a suffix
and imported as its own new node instead; the existing node is never
touched. Adoption stays a deliberate, per-node, human-reviewed action
in Catalog Mirror's UI.
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe import _
from frappe.utils.nestedset import rebuild_tree

from ecommerce_integrations.catalog_mirror.engine.adapters.base import LiveCategoryNode
from ecommerce_integrations.catalog_mirror.engine.adapters.shopware import ShopwareCatalogAdapter
from ecommerce_integrations.shopware6.connection import temp_shopware_session
from ecommerce_integrations.shopware6.constants import SETTING_DOCTYPE
from ecommerce_integrations.shopware6.utils import create_shopware_log, update_shopware_log


@frappe.whitelist()
def import_categories_from_shopware() -> dict[str, Any]:
    """Entry point for the "Kategorien aus Shopware importieren" button.

    Runs as a background job. A full tree walk (one HTTP round trip
    per category for children + a media lookup per node) reliably
    exceeds the web worker's request timeout on anything but a tiny
    catalog — and a killed synchronous request leaves no trace,
    nothing gets logged, nothing to retry. Progress is committed
    incrementally (per root, see ``_run_category_import``) and the
    final result is written to Ecommerce Integration Log instead of
    being returned inline.
    """
    frappe.only_for("System Manager")

    setting = frappe.get_doc(SETTING_DOCTYPE)
    if not setting.is_enabled():
        frappe.throw(_("Bitte zuerst die Shopware-Integration aktivieren"))

    log = create_shopware_log(
        status="Queued",
        method="category_import",
        message=_("Kategorie-Import wurde eingereiht..."),
        make_new=True,
    )

    frappe.enqueue(
        _run_category_import,
        queue="long",
        timeout=3600,
        job_name=f"shopware6_category_import_{log.name}",
        request_id=log.name,
        enqueue_after_commit=True,
    )

    return {"queued": True, "log": log.name}


def _run_category_import(request_id: str) -> None:
    stats: dict[str, Any] = {
        "created": 0,
        "updated": 0,
        "skipped_ignored": 0,
        "name_conflicts": 0,
        "images_set": 0,
        "nodes_seen": 0,
        "errors": [],
        # Diagnostics: one entry per Shopware absolute root actually
        # fetched, "<name> (<n> children)" — the fastest way to see
        # whether a given navigation/footer tree was reached at all
        # without guessing from the ERPNext side.
        "root_summaries": [],
    }

    # Every Item Group insert/save below fires the "Item Group" on_update
    # hook, which normally queues an *outbound* push back to Shopware
    # (shopware6.services.queue_hooks.queue_item_group_for_sync). That's
    # exactly backwards here — we're pulling categories that already
    # exist in Shopware, so pushing them straight back is both redundant
    # and, on a read-only-permissioned integration, a guaranteed 403
    # (category:create / category:update). Suppress it for the duration
    # of this job via the same flag the queue hook already checks.
    previous_skip_flag = getattr(frappe.flags, "skip_shopware_sync", False)
    frappe.flags.skip_shopware_sync = True
    try:
        setting = frappe.get_doc(SETTING_DOCTYPE)
        root_parent = _ensure_root_item_group(setting.category_sync_root or "Products")

        root_ids = _fetch_absolute_root_ids()
        stats["roots_found"] = len(root_ids)
        if not root_ids:
            stats["errors"].append("No categories found in Shopware.")

        adapter = ShopwareCatalogAdapter()
        for root_id in root_ids:
            tree_root = adapter.fetch_tree(root_id)
            if not tree_root:
                stats["errors"].append(f"Could not fetch category tree for root {root_id}.")
                continue
            stats["root_summaries"].append(f"{tree_root.name or root_id} ({len(tree_root.children)})")
            # The technical root itself isn't imported as an Item Group —
            # only its children are, parented under category_sync_root.
            # Mirrors the export side's "skip_root_category" convention.
            for child in tree_root.children:
                _import_node(child, parent_item_group=root_parent, stats=stats)
            # Commit after each root tree rather than only at the very
            # end — a job that dies partway (worker restart, OOM) still
            # keeps whatever it already finished instead of losing it
            # to a rollback.
            frappe.db.commit()

        # Item Group is a Frappe NestedSet doctype: the tree view reads
        # the internal lft/rgt columns, not parent_item_group directly.
        # Hundreds of inserts/reparents in one job can leave lft/rgt out
        # of sync with parent_item_group even though every individual
        # save() went through the normal ORM path — the symptom is
        # exactly "List View filtered by shopware_category_id shows
        # everything, Tree View shows almost nothing". Rebuild once at
        # the end of a run rather than after every single node (which
        # would be one full-tree recalculation per insert).
        if stats["created"] or stats["updated"]:
            rebuild_tree("Item Group")

        update_shopware_log(
            request_id,
            status="Success" if not stats["errors"] else "Error",
            message=_(
                "Wurzelbäume: {0} ({1}), besuchte Knoten: {2} — "
                "Erstellt: {3}, Aktualisiert: {4}, Bilder gesetzt: {5}, "
                "Übersprungen: {6}, Namenskonflikte (als neue Kategorie angelegt, "
                "bestehende unangetastet): {7}"
            ).format(
                stats["roots_found"], ", ".join(stats["root_summaries"]), stats["nodes_seen"],
                stats["created"], stats["updated"], stats["images_set"],
                stats["skipped_ignored"], stats["name_conflicts"],
            ),
            exception="\n".join(stats["errors"]) if stats["errors"] else None,
        )
    except Exception as e:
        update_shopware_log(request_id, status="Error", exception=str(e))
        raise
    finally:
        frappe.flags.skip_shopware_sync = previous_skip_flag


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
    stats["nodes_seen"] += 1

    if node.ignored:
        stats["skipped_ignored"] += 1
        return

    item_group_name = _resolve_item_group(node, parent_item_group, stats)

    # Commit periodically, not just per root — a deep single tree (the
    # common case: one big main-navigation root) would otherwise stay
    # one giant uncommitted transaction until it finished entirely.
    processed = stats["created"] + stats["updated"]
    if processed and processed % 20 == 0:
        frappe.db.commit()

    # Even when this node itself failed to resolve (logged in
    # stats["errors"]), still walk its children — attaching them one
    # level up under this node's own parent instead of silently
    # dropping the entire subtree because one ancestor errored. A
    # single duplicate-name collision further down the tree shouldn't
    # cost dozens of otherwise-fine descendant categories.
    for child in node.children:
        _import_node(child, parent_item_group=item_group_name or parent_item_group, stats=stats)


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

        base_name = node.name or _("Unnamed Category")
        existing = frappe.db.exists("Item Group", {"item_group_name": base_name})

        target_name = base_name
        if existing:
            # 2. Name already claimed — either by a hand-made group
            # (e.g. from WeClapp) or a different already-mapped
            # category. Either way: never reparent or overwrite fields
            # on a node we don't own. Disambiguate and import as a new,
            # separate node instead. An operator who actually wants to
            # link an existing hand-made group to this Shopware category
            # does so explicitly via Catalog Mirror's per-node Adopt.
            target_name = _unique_item_group_name(base_name, parent_item_group, node.external_id)
            stats["name_conflicts"] += 1

        # 3. New Item Group.
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


def _unique_item_group_name(base_name: str, parent_item_group: str, external_id: str) -> str:
    """Find a free Item Group name for a category whose plain name is
    already claimed by a different mapped category.

    Shopware categories are commonly duplicated by name under
    different parents (e.g. a "Motor" category under every vehicle
    brand). A truncated external_id was tried first but Shopware's
    UUIDs are time-ordered, so categories bulk-created close together
    share the same leading hex characters — the truncated suffix
    collided across siblings-in-name-only just as often as it
    disambiguated them. Try the parent context first (meaningful to a
    human), then the full external_id (guaranteed unique), then a
    numeric counter — actually checking each candidate rather than
    assuming any of them is free.
    """
    candidates = [
        f"{base_name} ({parent_item_group})",
        f"{base_name} ({external_id})",
    ]
    for candidate in candidates:
        if not frappe.db.exists("Item Group", candidate):
            return candidate

    n = 2
    while frappe.db.exists("Item Group", f"{base_name} ({parent_item_group}) #{n}"):
        n += 1
    return f"{base_name} ({parent_item_group}) #{n}"


def _apply_fields(ig, node: LiveCategoryNode, parent_item_group: str) -> None:
    ig.parent_item_group = parent_item_group

    # Shopware's tree fetch for a node can transiently come back with
    # zero children (partial page, API hiccup) even though ERPNext
    # already has real child Item Groups linked underneath it from an
    # earlier successful run. Flipping is_group to 0 in that case trips
    # ERPNext's own "cannot be a leaf, has children" validation — and
    # rightly so, since demoting a node that actually has children
    # would orphan them. Only ever grow into a group, never shrink out
    # of one based on a single fetch.
    already_has_children = bool(ig.name) and frappe.db.exists(
        "Item Group", {"parent_item_group": ig.name}
    )
    ig.is_group = 1 if (node.children or already_has_children) else 0

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
    # Query-string associations (``category/{id}?associations[media]=``)
    # are rejected outright by newer lib_shopware6_api_base versions —
    # validate_endpoint only allows alphanumerics, hyphens, underscores,
    # dots and slashes in the path. Use the same POST-to-search pattern
    # the rest of the codebase already relies on for association fetches
    # instead of a GET with bracketed query params.
    response = client.request_post(
        "search/category",
        {
            "filter": [{"type": "equals", "field": "id", "value": category_id}],
            "associations": {"media": {}},
            "limit": 1,
        },
    )
    results = response.data or []
    if not results:
        return None
    media = results[0].get("media") or {}
    return media.get("url")
