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
commonly have many such trees, one main-navigation and one
footer-navigation root per sales channel/domain; importing every
root's subtree is what "all categories" means here. Each root becomes
its own Item Group (named after Shopware's own category name for it,
e.g. "teile-fundgrube.de", "Footer GFVerlag") parented under
``category_sync_root``, with that tree's real categories nested
underneath — keeping every sales channel's tree visibly distinct
instead of merging all of them into one flat bucket under the shared
root.

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
        "renamed": 0,
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
        # Every Shopware root tree gets corralled under one clearly
        # labelled, top-level "Shopware" node — a sibling of the
        # operator's native catalogue root(s), not nested inside one
        # of them. category_sync_root is a legacy/fallback setting
        # shared with the old export-direction sync and commonly
        # points at ERPNext's own generic "Products" group; nesting
        # "Shopware" inside it made the import look like a subset of
        # the operator's native catalogue instead of a clearly
        # separate, importer-owned boundary.
        root_parent = _ensure_root_item_group("Shopware")

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
            # Each Shopware absolute root is its own real Item Group —
            # e.g. "teile-fundgrube.de", "Footer GFVerlag" — parented
            # under category_sync_root, with that tree's actual
            # categories nested underneath it. Merging every root's
            # children directly into one shared category_sync_root
            # (the old behaviour) flattened all 16 navigation/footer
            # trees from every sales channel into one bucket with no
            # indication of which shop or tree a category came from.
            # _import_node already handles name collisions safely, so
            # importing the root itself needs no special-casing.
            _import_node(tree_root, parent_item_group=root_parent, breadcrumb=[], stats=stats)
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
        # would be one full-tree recalculation per insert). Unconditional
        # — _ensure_root_item_group can also reparent the "Shopware"
        # node itself via a direct db.set_value (no NestedSet hook),
        # which wouldn't be reflected by created/updated counts alone.
        rebuild_tree("Item Group")

        update_shopware_log(
            request_id,
            status="Success" if not stats["errors"] else "Error",
            message=_(
                "Wurzelbäume: {0} ({1}), besuchte Knoten: {2} — "
                "Erstellt: {3}, Aktualisiert: {4}, Umbenannt (lesbarerer Pfad): {5}, "
                "Bilder gesetzt: {6}, Übersprungen: {7}, Namenskonflikte (als neue "
                "Kategorie angelegt, bestehende unangetastet): {8}"
            ).format(
                stats["roots_found"], ", ".join(stats["root_summaries"]), stats["nodes_seen"],
                stats["created"], stats["updated"], stats["renamed"], stats["images_set"],
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
    true_root = frappe.db.get_value(
        "Item Group", {"is_group": 1, "parent_item_group": ["in", ["", None]]}, "name"
    )

    if frappe.db.exists("Item Group", name):
        # Fix up a node that already exists but ended up nested under
        # something else on an earlier run (e.g. "Shopware" used to be
        # created one level inside category_sync_root) — this function's
        # whole contract is "make sure this is a top-level node."
        current_parent = frappe.db.get_value("Item Group", name, "parent_item_group")
        if true_root and current_parent != true_root:
            frappe.db.set_value("Item Group", name, "parent_item_group", true_root)
        return name

    return _ensure_item_group(name, true_root)


def _ensure_item_group(name: str, parent_item_group: str) -> str:
    if frappe.db.exists("Item Group", name):
        return name

    ig = frappe.new_doc("Item Group")
    ig.item_group_name = name
    ig.is_group = 1
    ig.parent_item_group = parent_item_group
    ig.insert(ignore_permissions=True)
    return ig.name


def _import_node(
    node: LiveCategoryNode, parent_item_group: str, breadcrumb: list[str], stats: dict[str, Any],
) -> None:
    stats["nodes_seen"] += 1

    if node.ignored:
        stats["skipped_ignored"] += 1
        return

    item_group_name = _resolve_item_group(node, parent_item_group, breadcrumb, stats)

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
    child_breadcrumb = [*breadcrumb, node.name or node.external_id]
    for child in node.children:
        _import_node(
            child, parent_item_group=item_group_name or parent_item_group,
            breadcrumb=child_breadcrumb, stats=stats,
        )


def _resolve_item_group(
    node: LiveCategoryNode, parent_item_group: str, breadcrumb: list[str], stats: dict[str, Any],
) -> str | None:
    try:
        base_name = node.name or _("Unnamed Category")

        # 1. Already imported on a previous run of this script.
        linked_name = frappe.db.get_value(
            "Item Group", {"shopware_category_id": node.external_id}, "name"
        )
        if linked_name:
            desired_name = _desired_item_group_name(base_name, breadcrumb, node.external_id, linked_name)
            if desired_name != linked_name:
                # Fix up a name that was disambiguated under the old
                # scheme (bare parent name, or a raw UUID) into the
                # current, human-readable breadcrumb form. Item Group
                # is otherwise ours to manage once shopware_category_id
                # is set, and frappe.rename_doc keeps every Item
                # reference (item_group, additional_item_groups) intact.
                try:
                    frappe.rename_doc("Item Group", linked_name, desired_name, ignore_permissions=True)
                    linked_name = desired_name
                    stats["renamed"] += 1
                except Exception:
                    frappe.log_error(
                        title="Shopware category import: rename failed",
                        message=frappe.get_traceback(),
                    )
            ig = frappe.get_doc("Item Group", linked_name)
            _apply_fields(ig, node, parent_item_group)
            ig.save(ignore_permissions=True)
            stats["updated"] += 1
            _maybe_set_image(ig, node, stats)
            return ig.name

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
            target_name = _unique_item_group_name(base_name, breadcrumb, node.external_id)
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


def _desired_item_group_name(
    base_name: str, breadcrumb: list[str], external_id: str, current_name: str,
) -> str:
    """What an already-linked node's name should be right now — plain
    base_name if that's free (or already this node's own), otherwise
    the same breadcrumb disambiguation a brand-new node would get.
    """
    existing = frappe.db.get_value(
        "Item Group", {"item_group_name": base_name}, ["name", "shopware_category_id"], as_dict=True,
    )
    if not existing or existing.name == current_name:
        return base_name
    if existing.shopware_category_id == external_id:
        return base_name
    return _unique_item_group_name(base_name, breadcrumb, external_id)


def _format_breadcrumb(breadcrumb: list[str]) -> str:
    segments = ["SW", *[b for b in breadcrumb if b]]
    path = "/".join(segments)
    # Item Group names are capped at 140 chars total — a deep tree
    # could otherwise blow that budget on the breadcrumb alone. Root +
    # immediate parent are the most useful for placing the category at
    # a glance, so trim the middle first rather than the ends.
    if len(path) > 90 and len(segments) > 3:
        path = "/".join([segments[0], segments[1], "…", segments[-1]])
    return path


def _unique_item_group_name(base_name: str, breadcrumb: list[str], external_id: str) -> str:
    """Find a free Item Group name for a category whose plain name is
    already claimed by a different mapped category.

    Shopware categories are commonly duplicated by name across
    completely different trees (e.g. a "Bremsleitung" category under
    both "francetec.de/2CV6" and some other domain/model) — a single
    immediate-parent name isn't reliably unique either, and a raw
    external_id is unreadable. Disambiguate with the full human-
    readable breadcrumb (Shopware's own category names, root tree down
    to the immediate parent) instead — e.g.
    "Bremsleitung (SW/francetec.de/2CV6/Bremsanlage)" tells the
    operator exactly where in which tree this lives without opening
    the tree view. Falls back to appending the external_id, then a
    numeric counter, only in the (very unlikely) case the full path
    itself collides too.
    """
    path = _format_breadcrumb(breadcrumb)
    candidates = [
        f"{base_name} ({path})",
        f"{base_name} ({path}/{external_id[:8]})",
    ]
    for candidate in candidates:
        candidate = candidate[:140]
        if not frappe.db.exists("Item Group", candidate):
            return candidate

    n = 2
    while True:
        candidate = f"{base_name} ({path}) #{n}"[:140]
        if not frappe.db.exists("Item Group", candidate):
            return candidate
        n += 1


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

    ig.shopware_active = 1 if node.active else 0

    # Item Group has no native "description" field in ERPNext v16 —
    # writing to ig.description would silently discard the value
    # (Frappe accepts setting an unknown attribute on a Document but
    # never persists it, no error). category_description is our own
    # custom field that actually gets saved.
    meta = frappe.get_meta("Item Group")
    if node.description and meta.has_field("category_description"):
        ig.category_description = node.description
    if node.meta_title and meta.has_field("seo_title"):
        ig.seo_title = node.meta_title
    if node.meta_description and meta.has_field("seo_meta_description"):
        ig.seo_meta_description = node.meta_description
    if node.meta_keywords and meta.has_field("seo_keywords"):
        ig.seo_keywords = node.meta_keywords


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
