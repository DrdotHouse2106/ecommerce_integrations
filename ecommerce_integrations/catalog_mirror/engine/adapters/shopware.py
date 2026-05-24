"""Shopware CatalogAdapter — Catalog Mirror ↔ Shopware category tree.

Mirrors the Smart Collections Shopware adapter's session pattern
(``temp_shopware_session`` decorator, one idempotency key per call).
The contract is node-level (one upsert, one move, one delete per call)
so the orchestrator can walk the tree in dependency order and surface
per-node failures without losing the rest of the run.

Tree walks are paginated under ``POST /search/category`` with a
``parentId`` filter; cycles are guarded by a visited-id set even though
Shopware shouldn't produce them.
"""

from __future__ import annotations

import json

from ecommerce_integrations.catalog_mirror.constants import BACKEND_SHOPWARE
from ecommerce_integrations.catalog_mirror.engine.adapters.base import (
    AdapterError,
    CatalogAdapter,
    LiveCategoryNode,
    NodeMatch,
    register,
)
from ecommerce_integrations.shopware6.connection import temp_shopware_session

_TREE_PAGE_SIZE = 200
_MATCH_LIMIT = 25


@register(BACKEND_SHOPWARE)
class ShopwareCatalogAdapter(CatalogAdapter):
    def fetch_tree(
        self, root_external_id: str | None,
    ) -> LiveCategoryNode | None:
        return _with_client(self._fetch_tree_impl, root_external_id)

    def find_matching_nodes(
        self, name: str, parent_external_id: str | None,
    ) -> list[NodeMatch]:
        return _with_client(self._find_matching_impl, name, parent_external_id)

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
        return _with_client(
            self._upsert_impl,
            external_id=external_id,
            parent_external_id=parent_external_id,
            name=name,
            description=description,
            active=active,
            target_sales_channel=target_sales_channel,
        )

    def move_node(
        self, external_id: str, new_parent_external_id: str | None,
    ) -> None:
        _with_client(self._move_impl, external_id, new_parent_external_id)

    def delete_node(self, external_id: str) -> None:
        _with_client(self._delete_impl, external_id)

    # ----- implementations (run inside the session decorator) --------

    def _fetch_tree_impl(
        self, client, root_external_id: str | None,
    ) -> LiveCategoryNode | None:
        # Resolve the root — when the operator hasn't pinned an
        # external_root_id yet we fall back to the sales-channel's
        # navigationCategoryId. Either way the rest of the walk is the
        # same shape.
        root_id = root_external_id
        if not root_id:
            # No explicit root and no sales-channel context here — the
            # orchestrator will normally pre-resolve via the
            # target_sales_channel; absent that we report None and let
            # the preview flag ``unresolved_external_root=True``.
            return None

        root_attrs = self._fetch_category(client, root_id)
        if root_attrs is None:
            return None

        root = LiveCategoryNode(
            external_id=root_id,
            name=root_attrs.get("name") or "",
            parent_external_id=root_attrs.get("parentId"),
            description=root_attrs.get("description"),
            active=bool(root_attrs.get("active", True)),
        )

        # BFS the descendants. Visited-id guard guards against the
        # theoretical cycle case so a malformed tree can't hang the
        # walk.
        visited: set[str] = {root_id}
        index: dict[str, LiveCategoryNode] = {root_id: root}
        frontier: list[str] = [root_id]

        while frontier:
            parent_id = frontier.pop(0)
            children = self._fetch_children(client, parent_id)
            for child_attrs in children:
                child_id = child_attrs.get("id")
                if not child_id or child_id in visited:
                    continue
                visited.add(child_id)
                node = LiveCategoryNode(
                    external_id=child_id,
                    name=child_attrs.get("name") or "",
                    parent_external_id=parent_id,
                    description=child_attrs.get("description"),
                    active=bool(child_attrs.get("active", True)),
                )
                index[child_id] = node
                index[parent_id].children.append(node)
                frontier.append(child_id)

        # Populate ``product_count`` per category in one batched
        # request against the product_category join entity. Without
        # this the preview's "Produkte" column is always 0, which lies
        # to the operator about whether deleting an orphan would
        # detach products.
        counts = self._fetch_product_counts(client, list(index.keys()))
        for cid, node in index.items():
            node.product_count = counts.get(cid, 0)

        return root

    def _fetch_product_counts(
        self, client, category_ids: list[str],
    ) -> dict[str, int]:
        """Return ``{categoryId: count}`` over the ``product`` entity's
        ``categoryIds`` array field.

        We can't aggregate on ``product_category`` (the join table has
        no scalar ``id``, so Shopware rejects the search with
        ``FRAMEWORK__UNMAPPED_FIELD``). Instead aggregate by
        ``product.categoryIds`` — a list field whose terms aggregation
        produces one bucket per *combination* of categories a product
        belongs to. Each bucket key is the JSON-serialised list
        (e.g. ``'["uuid-a","uuid-b"]'``); we parse it and accumulate
        the count against every contained category id so products
        assigned to multiple categories are counted in each.
        """
        if not category_ids:
            return {}
        wanted = set(category_ids)
        counts: dict[str, int] = {cid: 0 for cid in category_ids}
        try:
            resp = client.request_post(
                "search/product",
                payload={
                    "limit": 1,
                    "aggregations": [
                        {
                            "type": "terms",
                            "name": "by_category",
                            "field": "categoryIds",
                            # Shopware caps the bucket count — keep it
                            # generous so big trees with many products
                            # come back fully resolved in one call.
                            "limit": max(5000, len(category_ids) * 2),
                        },
                    ],
                },
            )
        except Exception:
            return counts

        buckets = ((resp or {}).get("aggregations") or {}).get(
            "by_category", {},
        ).get("buckets") or []
        for entry in buckets:
            raw_key = entry.get("key") or ""
            cnt = int(entry.get("count") or 0)
            if not raw_key or not cnt:
                continue
            # The bucket key is the JSON-encoded categoryIds list. We
            # accept both the canonical ``'["…"]'`` shape and the
            # legacy ``"uuid,uuid"`` fallback older Shopware versions
            # may emit — both decode to a flat list of uuids.
            ids: list[str]
            if raw_key.startswith("["):
                try:
                    parsed = json.loads(raw_key)
                    ids = [str(x) for x in parsed if x]
                except Exception:
                    ids = []
            else:
                ids = [p.strip() for p in raw_key.split(",") if p.strip()]
            for cid in ids:
                if cid in wanted:
                    counts[cid] += cnt
        return counts

    def _fetch_category(self, client, external_id: str) -> dict | None:
        try:
            resp = client.request_get(f"category/{external_id}")
        except Exception as e:
            if "404" in str(e):
                return None
            raise AdapterError(
                f"Shopware category fetch failed: {e}",
            ) from e
        data = (resp or {}).get("data") or {}
        # Shopware responses sometimes nest under ``attributes``, sometimes
        # flat — accept both shapes the same way the Smart Collections
        # adapter does.
        return data.get("attributes") or data or None

    def _fetch_children(self, client, parent_id: str) -> list[dict]:
        results: list[dict] = []
        page = 1
        while True:
            criteria = {
                "page": page,
                "limit": _TREE_PAGE_SIZE,
                "filter": [
                    {"type": "equals", "field": "parentId", "value": parent_id},
                ],
                "includes": {
                    "category": [
                        "id",
                        "name",
                        "parentId",
                        "description",
                        "active",
                    ],
                },
            }
            try:
                resp = client.request_post("search/category", payload=criteria)
            except Exception as e:
                raise AdapterError(
                    f"Shopware category search failed: {e}",
                ) from e
            data = (resp or {}).get("data") or []
            for row in data:
                attrs = row.get("attributes") or row
                ext_id = row.get("id") or attrs.get("id")
                if not ext_id:
                    continue
                results.append({
                    "id": ext_id,
                    "name": attrs.get("name"),
                    "parentId": attrs.get("parentId") or parent_id,
                    "description": attrs.get("description"),
                    "active": attrs.get("active", True),
                })
            if len(data) < _TREE_PAGE_SIZE:
                break
            page += 1
        return results

    def _find_matching_impl(
        self, client, name: str, parent_external_id: str | None,
    ) -> list[NodeMatch]:
        if not name:
            return []
        filters: list[dict] = [
            {"type": "equals", "field": "name", "value": name},
        ]
        if parent_external_id:
            filters.append(
                {
                    "type": "equals",
                    "field": "parentId",
                    "value": parent_external_id,
                },
            )
        criteria = {
            "limit": _MATCH_LIMIT,
            "filter": filters,
        }
        try:
            resp = client.request_post("search/category", payload=criteria)
        except Exception as e:
            raise AdapterError(
                f"Shopware category search failed: {e}",
            ) from e

        matches: list[NodeMatch] = []
        for row in (resp or {}).get("data") or []:
            attrs = row.get("attributes") or row
            ext_id = row.get("id") or attrs.get("id")
            if not ext_id:
                continue
            breadcrumb = attrs.get("breadcrumb") or [attrs.get("name") or name]
            matches.append(
                NodeMatch(
                    external_id=ext_id,
                    name=attrs.get("name") or name,
                    path=" / ".join(str(p) for p in breadcrumb),
                    linked_product_count=0,
                ),
            )
        return matches

    def _upsert_impl(
        self,
        client,
        *,
        external_id: str | None,
        parent_external_id: str | None,
        name: str,
        description: str | None,
        active: bool,
        target_sales_channel: str | None,
    ) -> str:
        payload: dict = {
            "name": name,
            "active": bool(active),
            "type": "page",
            "displayNestedProducts": True,
            "visible": True,
        }
        if parent_external_id:
            payload["parentId"] = parent_external_id
        if description:
            payload["description"] = description

        if external_id:
            try:
                client.request_patch(
                    f"category/{external_id}", payload=payload,
                )
            except Exception as e:
                if "404" not in str(e):
                    raise AdapterError(
                        f"Shopware category update failed: {e}",
                    ) from e
                # Fall through to create when the recorded id is stale.
                external_id = None

        if not external_id:
            # Shopware POST /api/category returns 204 No Content by
            # default; the body is empty and the lib_shopware6_api_base
            # client rejects ``?_response=true`` query strings. So we
            # supply the id ourselves in the payload — Shopware accepts
            # any uuid4-shaped string and writes the row under it.
            import uuid as _uuid
            new_id = _uuid.uuid4().hex
            payload["id"] = new_id
            try:
                client.request_post("category", payload=payload)
            except Exception as e:
                raise AdapterError(
                    f"Shopware category create failed: {e}",
                ) from e
            external_id = new_id

        self._set_sales_channel_assignments(
            client, external_id, target_sales_channel,
        )
        return external_id

    def _set_sales_channel_assignments(
        self, client, external_id: str, target_sales_channel: str | None,
    ) -> None:
        # For Catalog Mirror the storefront visibility is inherited
        # through the navigation tree (root category configured on the
        # sales channel), so this assignment is a best-effort hint —
        # not load-bearing. 409 = already linked; 404 = Shopware
        # rejects the operation as nonsensical for a category whose
        # visibility is already covered via the navigation root. Both
        # are idempotent no-ops for our use case.
        if not target_sales_channel:
            return
        try:
            client.request_post(
                f"category/{external_id}/sales-channels",
                payload={"id": target_sales_channel},
            )
        except Exception as e:
            msg = str(e)
            if "409" in msg or "404" in msg:
                return
            # Don't raise: the category already exists in Shopware
            # with the correct parent — failing here would prevent
            # the local mapping from being persisted and leave us
            # creating the same category on every run.
            try:
                import frappe as _f
                _f.logger("catalog_mirror").warning(
                    f"Shopware sales-channel assign skipped for "
                    f"{external_id}: {msg[:200]}"
                )
            except Exception:  # noqa: BLE001
                pass

    def _move_impl(
        self,
        client,
        external_id: str,
        new_parent_external_id: str | None,
    ) -> None:
        payload: dict = {"parentId": new_parent_external_id}
        try:
            client.request_patch(
                f"category/{external_id}", payload=payload,
            )
        except Exception as e:
            raise AdapterError(
                f"Shopware category move failed: {e}",
            ) from e

    def _delete_impl(self, client, external_id: str) -> None:
        try:
            client.request_delete(f"category/{external_id}")
        except Exception as e:
            if "404" in str(e):
                return
            raise AdapterError(
                f"Shopware category delete failed: {e}",
            ) from e


def _with_client(fn, *args, **kwargs):
    """Run ``fn`` inside a Shopware admin session.

    The decorator ``temp_shopware_session`` injects the client as the
    first positional arg; in test mode (``frappe.flags.in_test``) the
    decorator skips auth and calls ``fn`` without the client so tests
    can pass their own mock.
    """

    @temp_shopware_session
    def runner(client, *inner_args, **inner_kwargs):
        return fn(client, *inner_args, **inner_kwargs)

    return runner(*args, **kwargs)


def resolve_sales_channel_root(sales_channel_id: str) -> str | None:
    """Return the navigationCategoryId for ``sales_channel_id`` or None.

    Exposed as a module-level helper so the orchestrator can pre-resolve
    the external root for a fresh mirror without needing to instantiate
    the adapter just to peek at one field. The Shopware sales-channel
    endpoint is cheap (one GET); the adapter still owns the session via
    the decorator.
    """
    if not sales_channel_id:
        return None

    @temp_shopware_session
    def _runner(client):
        try:
            resp = client.request_get(f"sales-channel/{sales_channel_id}")
        except Exception:
            return None
        data = (resp or {}).get("data") or {}
        attrs = data.get("attributes") or data
        return attrs.get("navigationCategoryId")

    return _runner()
