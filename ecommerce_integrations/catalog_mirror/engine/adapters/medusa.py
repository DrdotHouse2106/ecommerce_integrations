"""Medusa CatalogAdapter — Catalog Mirror ↔ Medusa product-category tree.

Mirrors the Shopware adapter's node-level contract (one upsert, one
move, one delete per call) so the orchestrator can walk the tree in
dependency order. The session/HTTP idioms are borrowed from the
Smart Collections Medusa adapter — same ``optional_session`` /
``medusa_request`` plumbing, same handle-collision recovery, same
404-is-silent treatment for deletes.

Key differences vs the Shopware adapter:

- Medusa product-categories have **no per-channel association**. The
  ``target_sales_channel`` argument is accepted for interface parity
  but intentionally ignored — there is no Medusa-side equivalent of
  Shopware's ``/category/{id}/sales-channels`` endpoint.
- Identity is by ``id`` but discovery is by ``handle`` (URL-safe slug).
  ``find_matching_nodes`` searches both ``name`` (via the ``q`` free-text
  parameter) and ``handle`` so an operator who renamed an item group can
  still adopt the existing Medusa category.
- The parent link is the ``parent_category_id`` column — the same field
  is used in both the tree filter (``GET ?parent_category_id=...``) and
  the upsert/move payloads.
"""

from __future__ import annotations

import re
import unicodedata

from ecommerce_integrations.catalog_mirror.constants import BACKEND_MEDUSA
from ecommerce_integrations.catalog_mirror.engine.adapters.base import (
    AdapterError,
    CatalogAdapter,
    LiveCategoryNode,
    NodeMatch,
    register,
)
from ecommerce_integrations.medusa.connection import (
    medusa_request,
    optional_session,
)
from ecommerce_integrations.medusa.constants import API_CATEGORIES


_TREE_PAGE_SIZE = 200
_MATCH_LIMIT = 25
_FIELDS = "id,name,handle,description,is_active,parent_category_id"
_MAX_DEPTH = 8
_MAX_NODES = 10_000

_GERMAN_UMLAUT_MAP = str.maketrans(
    {
        "ä": "ae",
        "ö": "oe",
        "ü": "ue",
        "ß": "ss",
        "Ä": "Ae",
        "Ö": "Oe",
        "Ü": "Ue",
    }
)


def _slugify(value: str) -> str:
    """Generate a Medusa-style handle from ``value``.

    Mirrors the ``slugify`` helper in the Smart Collections module so a
    handle generated here is byte-identical to one generated there for
    the same input — relevant when an operator already curated Medusa
    categories via Smart Collections and now adopts them with a mirror.
    """
    if not value:
        return ""
    value = value.translate(_GERMAN_UMLAUT_MAP)
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    value = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return value


@register(BACKEND_MEDUSA)
class MedusaCatalogAdapter(CatalogAdapter):
    def fetch_tree(
        self, root_external_id: str | None,
    ) -> LiveCategoryNode | None:
        # Unlike Shopware (where the sales-channel's ``navigationCategoryId``
        # gives a canonical root), Medusa has no nav-root concept derived
        # from a sales channel. The operator must pin ``external_root_id``
        # explicitly; absent that we surface ``unresolved_external_root``.
        if not root_external_id:
            return None

        with optional_session() as (session, base_url):
            root_attrs = self._fetch_category(session, base_url, root_external_id)
            if root_attrs is None:
                return None

            root = LiveCategoryNode(
                external_id=root_external_id,
                name=root_attrs.get("name") or "",
                parent_external_id=root_attrs.get("parent_category_id"),
                description=root_attrs.get("description"),
                active=bool(root_attrs.get("is_active", True)),
            )

            # BFS the descendants with a depth + total-node cap so a
            # malformed tree (cycle, runaway nesting) can't hang the walk.
            visited: set[str] = {root_external_id}
            index: dict[str, LiveCategoryNode] = {root_external_id: root}
            # Each frontier entry: (parent_external_id, depth_of_children)
            frontier: list[tuple[str, int]] = [(root_external_id, 1)]
            total_nodes = 1

            while frontier:
                parent_id, depth = frontier.pop(0)
                if depth > _MAX_DEPTH:
                    self._log_warning(
                        f"Medusa fetch_tree: depth cap {_MAX_DEPTH} reached "
                        f"under {parent_id!r}; stopping descent",
                    )
                    continue

                for child_attrs in self._fetch_children(session, base_url, parent_id):
                    child_id = child_attrs.get("id")
                    if not child_id or child_id in visited:
                        continue
                    visited.add(child_id)
                    node = LiveCategoryNode(
                        external_id=child_id,
                        name=child_attrs.get("name") or "",
                        parent_external_id=parent_id,
                        description=child_attrs.get("description"),
                        active=bool(child_attrs.get("is_active", True)),
                    )
                    index[child_id] = node
                    index[parent_id].children.append(node)
                    frontier.append((child_id, depth + 1))
                    total_nodes += 1

                    if total_nodes >= _MAX_NODES:
                        self._log_warning(
                            f"Medusa fetch_tree: node cap {_MAX_NODES} reached; "
                            f"stopping walk",
                        )
                        frontier.clear()
                        break

            return root

    def find_matching_nodes(
        self, name: str, parent_external_id: str | None,
    ) -> list[NodeMatch]:
        if not name:
            return []
        handle = _slugify(name)

        with optional_session() as (session, base_url):
            # Two queries: one by free-text name (``q``), one by handle.
            # Medusa's ``q`` is a substring/loose match so the name hit
            # surfaces renamed categories; the handle hit catches the
            # exact-slug case (and overlaps when the name was never
            # changed). Dedup happens after both queries return.
            rows: dict[str, dict] = {}
            for params in self._match_param_sets(name, handle, parent_external_id):
                try:
                    resp = medusa_request(
                        session, base_url, "GET", API_CATEGORIES,
                        params=params,
                    )
                except Exception as e:
                    if "404" in str(e):
                        continue
                    raise AdapterError(
                        f"Medusa category search failed: {e}",
                    ) from e
                for cat in (resp or {}).get("product_categories") or []:
                    ext_id = cat.get("id")
                    if not ext_id or ext_id in rows:
                        continue
                    rows[ext_id] = cat

        matches: list[NodeMatch] = []
        for ext_id, cat in list(rows.items())[:_MATCH_LIMIT]:
            matches.append(
                NodeMatch(
                    external_id=ext_id,
                    name=cat.get("name") or name,
                    path=cat.get("handle") or handle,
                    linked_product_count=0,
                ),
            )
        return matches

    def upsert_node(
        self,
        *,
        external_id: str | None,
        parent_external_id: str | None,
        name: str,
        description: str | None,
        active: bool,
        target_sales_channel: str | None,
    ) -> str:
        # ``target_sales_channel`` is accepted for interface parity with
        # the Shopware adapter but Medusa product-categories have no
        # per-channel association — there's no equivalent endpoint.
        del target_sales_channel  # explicitly unused

        handle = _slugify(name)
        payload: dict = {
            "name": name,
            "handle": handle,
            "is_active": bool(active),
        }
        if parent_external_id is not None:
            payload["parent_category_id"] = parent_external_id
        if description:
            payload["description"] = description

        with optional_session() as (session, base_url):
            if external_id:
                # Update path. Medusa doesn't reject ``handle`` on an
                # update that doesn't change it, so we include the same
                # body for create and update.
                try:
                    medusa_request(
                        session, base_url, "POST",
                        f"{API_CATEGORIES}/{external_id}",
                        json=payload,
                    )
                    return external_id
                except Exception as e:
                    # 404 → cached external_id is stale; fall through to
                    # create. Other errors are real failures.
                    if "404" not in str(e):
                        raise AdapterError(
                            f"Medusa category update failed: {e}",
                        ) from e

            # Create path.
            try:
                resp = medusa_request(
                    session, base_url, "POST", API_CATEGORIES,
                    json=payload,
                )
            except Exception as e:
                # ``handle already exists`` means a previous run created
                # the category but failed before persisting the id locally.
                # Recover by GET-by-handle — same pattern as the Smart
                # Collections Medusa adapter.
                if "already exists" in _error_body(e).lower():
                    existing = _lookup_by_handle(session, base_url, handle)
                    if existing:
                        return existing
                raise AdapterError(
                    f"Medusa category create failed: {e}",
                ) from e

            category = (resp or {}).get("product_category") or {}
            new_id = category.get("id")
            if not new_id:
                raise AdapterError(
                    f"Medusa category create returned no id: {resp!r}",
                )
            return new_id

    def move_node(
        self, external_id: str, new_parent_external_id: str | None,
    ) -> None:
        # Medusa accepts a partial update via POST; only the parent link
        # is touched so reparents stay distinguishable from name/active
        # drift in the integration log.
        payload = {"parent_category_id": new_parent_external_id}
        with optional_session() as (session, base_url):
            try:
                medusa_request(
                    session, base_url, "POST",
                    f"{API_CATEGORIES}/{external_id}",
                    json=payload,
                )
            except Exception as e:
                raise AdapterError(
                    f"Medusa category move failed: {e}",
                ) from e

    def delete_node(self, external_id: str) -> None:
        with optional_session() as (session, base_url):
            try:
                medusa_request(
                    session, base_url, "DELETE",
                    f"{API_CATEGORIES}/{external_id}",
                )
            except Exception as e:
                if "404" in str(e):
                    return
                raise AdapterError(
                    f"Medusa category delete failed: {e}",
                ) from e

    # ----- internal helpers ------------------------------------------

    def _fetch_category(
        self, session, base_url, external_id: str,
    ) -> dict | None:
        try:
            resp = medusa_request(
                session, base_url, "GET",
                f"{API_CATEGORIES}/{external_id}",
                params={"fields": _FIELDS},
            )
        except Exception as e:
            if "404" in str(e):
                return None
            raise AdapterError(
                f"Medusa category fetch failed: {e}",
            ) from e
        return (resp or {}).get("product_category") or None

    def _fetch_children(
        self, session, base_url, parent_id: str,
    ) -> list[dict]:
        results: list[dict] = []
        offset = 0
        while True:
            params = {
                "parent_category_id": parent_id,
                "limit": _TREE_PAGE_SIZE,
                "offset": offset,
                "fields": _FIELDS,
            }
            try:
                resp = medusa_request(
                    session, base_url, "GET", API_CATEGORIES, params=params,
                )
            except Exception as e:
                raise AdapterError(
                    f"Medusa category children fetch failed: {e}",
                ) from e
            page = (resp or {}).get("product_categories") or []
            results.extend(page)
            if len(page) < _TREE_PAGE_SIZE:
                break
            offset += _TREE_PAGE_SIZE
        return results

    def _match_param_sets(
        self, name: str, handle: str, parent_external_id: str | None,
    ) -> list[dict]:
        common: dict = {"limit": _MATCH_LIMIT, "fields": _FIELDS}
        if parent_external_id:
            common["parent_category_id"] = parent_external_id

        sets: list[dict] = []
        # Name search (free-text). ``q`` is Medusa's standard free-text
        # parameter — substring match across the category's display fields.
        sets.append({**common, "q": name})
        # Handle search — exact slug match. Skip if the slug is empty or
        # identical to the name (single-word ASCII lowercase) since the
        # ``q`` query already covers that case.
        if handle and handle != name:
            sets.append({**common, "handle": handle})
        return sets

    def _log_warning(self, message: str) -> None:
        # Best-effort log; missing frappe (e.g. bare unit-test import path)
        # must not break the walk. The orchestrator's run record still
        # captures the truncated tree.
        try:
            import frappe

            frappe.log_error(message, "Catalog Mirror Medusa")
        except Exception:
            pass


def _lookup_by_handle(session, base_url, handle: str) -> str | None:
    """Return the Medusa product-category id for ``handle`` if it exists."""
    if not handle:
        return None
    try:
        resp = medusa_request(
            session, base_url, "GET", API_CATEGORIES,
            params={"handle": handle, "limit": 1, "fields": "id,handle"},
        )
    except Exception:
        return None
    categories = (resp or {}).get("product_categories") or []
    if categories:
        return categories[0].get("id")
    return None


def _error_body(exc: Exception) -> str:
    """Best-effort extract of the response body from a ``requests.HTTPError``."""
    resp = getattr(exc, "response", None)
    if resp is None:
        return str(exc)
    try:
        return resp.text or str(exc)
    except Exception:
        return str(exc)
