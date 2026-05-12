"""Medusa CategoryAdapter — Smart Collection ↔ Medusa product category.

Uses the Medusa Admin API:
- ``POST /admin/product-categories`` to create
- ``POST /admin/product-categories/{id}`` to update
- ``POST /admin/product-categories/{id}/products`` to link/unlink items
  (Medusa v2 batch endpoint accepts ``add`` and ``remove`` arrays)

Item code → Medusa product id is resolved via the existing
``tabEcommerce Item`` mapping ``ecommerce=Medusa`` (set up by the
Medusa product-export pipeline). Items not yet synced to Medusa are
skipped and returned as the unresolved set so the orchestrator can log
them but continue.
"""

import frappe

from ecommerce_integrations.medusa.connection import (
    medusa_request,
    medusa_request_all,
    optional_session,
)
from ecommerce_integrations.medusa.constants import API_CATEGORIES
from ecommerce_integrations.smart_collections.constants import BACKEND_MEDUSA
from ecommerce_integrations.smart_collections.engine.adapters.base import (
    AdapterError,
    CategoryAdapter,
    register,
)


_LINK_BATCH_SIZE = 50


@register(BACKEND_MEDUSA)
class MedusaCategoryAdapter(CategoryAdapter):
    def upsert_category(self, collection, target) -> str:
        # ``external_handle`` lets operators override the channel-suffixed
        # slug with a shorter storefront-friendly URL handle. Falls back to
        # slug so existing collections keep their handles.
        handle = (
            (getattr(collection, "external_handle", None) or "").strip()
            or collection.slug
        )
        payload = {
            "name": collection.title,
            "handle": handle,
            "is_active": bool(collection.is_active),
        }
        if collection.description:
            payload["description"] = collection.description

        # Propagate hierarchy: if the collection points at a parent
        # Smart Collection, find that parent's Medusa target for the
        # *same* sales channel and use its external_id. Parents that
        # haven't been synced yet (or that don't target this channel)
        # fall back to a root-level category — the sync re-runs daily,
        # so on the next pass the parent will exist and the child gets
        # promoted into the tree.
        parent_id = _resolve_parent_category_id(collection, target)
        if parent_id:
            payload["parent_category_id"] = parent_id

        with optional_session() as (session, base_url):
            if target.external_id:
                try:
                    medusa_request(
                        session, base_url, "POST",
                        f"{API_CATEGORIES}/{target.external_id}",
                        json=payload,
                    )
                    return target.external_id
                except Exception as e:
                    # If Medusa returned 404 the cached external_id is stale
                    # — fall through to a create.
                    if "404" not in str(e):
                        raise AdapterError(f"Medusa category update failed: {e}") from e

            try:
                resp = medusa_request(
                    session, base_url, "POST", API_CATEGORIES, json=payload,
                )
            except Exception as e:
                # Medusa returns 400 ``Product category with handle ...
                # already exists.`` when a previous create-then-link-fail
                # cycle left an orphan category in Medusa with no
                # ``external_id`` persisted on the target. The exception
                # ``str()`` only carries the status line, not the body —
                # check the response body via ``e.response.text`` and fall
                # back to a lookup-by-handle so the sync self-heals.
                resp_text = ""
                resp = getattr(e, "response", None)
                if resp is not None:
                    try:
                        resp_text = resp.text or ""
                    except Exception:
                        resp_text = ""
                if "already exists" in resp_text.lower():
                    existing = _lookup_by_handle(session, base_url, handle)
                    if existing:
                        return existing
                raise AdapterError(f"Medusa category create failed: {e}") from e

        category = (resp or {}).get("product_category") or {}
        external_id = category.get("id")
        if not external_id:
            raise AdapterError(
                f"Medusa category create returned no id: {resp!r}"
            )
        return external_id

    def link_items(self, target, item_codes: list[str]) -> list[str]:
        return self._mutate_links(target, add=item_codes, remove=())

    def unlink_items(self, target, item_codes: list[str]) -> None:
        self._mutate_links(target, add=(), remove=item_codes)

    def delete_category(self, target) -> None:
        if not target.external_id:
            return
        with optional_session() as (session, base_url):
            try:
                medusa_request(
                    session, base_url, "DELETE",
                    f"{API_CATEGORIES}/{target.external_id}",
                )
            except Exception as e:
                raise AdapterError(f"Medusa category delete failed: {e}") from e

    def _mutate_links(
        self, target, *, add: list[str] | tuple[str, ...], remove: list[str] | tuple[str, ...]
    ) -> list[str]:
        if not target.external_id:
            raise AdapterError(
                "link/unlink called before upsert_category — external_id is empty"
            )

        product_ids_add, missing = _items_to_medusa_product_ids(add)
        product_ids_remove, _ = _items_to_medusa_product_ids(remove)

        # Medusa v2 batch endpoint: POST {add: [...], remove: [...]}.
        # Earlier code sent ``{product_ids: [...]}`` which Medusa rejects
        # with ``{"type":"invalid_data","message":"Unrecognized fields:
        # 'product_ids'"}`` — a 400 that surfaced as the cryptic "Medusa
        # link batch failed" message in the integration log.
        url = f"{API_CATEGORIES}/{target.external_id}/products"

        with optional_session() as (session, base_url):
            for batch in _chunked(product_ids_add, _LINK_BATCH_SIZE):
                try:
                    medusa_request(
                        session, base_url, "POST", url,
                        json={"add": batch, "remove": []},
                    )
                except Exception as e:
                    raise AdapterError(f"Medusa link batch failed: {e}") from e

            for batch in _chunked(product_ids_remove, _LINK_BATCH_SIZE):
                try:
                    medusa_request(
                        session, base_url, "POST", url,
                        json={"add": [], "remove": batch},
                    )
                except Exception as e:
                    raise AdapterError(f"Medusa unlink batch failed: {e}") from e

        return missing


def _resolve_parent_category_id(collection, target) -> str | None:
    """Return the parent collection's external_id on the same sales channel.

    Hierarchy is per-channel: ``Steckregal`` and ``Steckregal-Zubehör``
    might both have Medusa targets, but only the same-channel target's
    external_id makes sense as a parent in that channel's category tree.
    """
    if not getattr(collection, "parent_collection", None):
        return None
    row = frappe.db.sql(
        """
        SELECT external_id
        FROM `tabEcommerce Smart Collection Target`
        WHERE parent = %s AND backend = %s AND sales_channel = %s
          AND external_id IS NOT NULL AND external_id != ''
        LIMIT 1
        """,
        (collection.parent_collection, target.backend, target.sales_channel),
    )
    return row[0][0] if row else None


def _lookup_by_handle(session, base_url, handle: str) -> str | None:
    """Return the Medusa product-category id for ``handle`` if it exists.

    Used by ``upsert_category`` as a fallback when Medusa rejects a create
    with "already exists" — the orphaned category from a previous
    create-then-link-fail run still lives in Medusa but the local target
    row never got its ``external_id`` persisted.
    """
    try:
        resp = medusa_request(
            session, base_url, "GET", API_CATEGORIES,
            params={"handle": handle, "limit": 1},
        )
    except Exception:
        return None
    categories = (resp or {}).get("product_categories") or []
    if categories:
        return categories[0].get("id")
    return None


def _items_to_medusa_product_ids(item_codes) -> tuple[list[str], list[str]]:
    """Map ERPNext item codes to Medusa product ids via ``Item.medusa_product_id``.

    Returns ``(resolved_product_ids, missing_item_codes)``.
    """
    if not item_codes:
        return [], []
    rows = frappe.db.sql(
        """
        SELECT name, medusa_product_id
        FROM `tabItem`
        WHERE name IN ({placeholders})
        """.format(placeholders=", ".join(["%s"] * len(item_codes))),
        tuple(item_codes),
        as_dict=True,
    )
    resolved: list[str] = []
    have: set[str] = set()
    for r in rows:
        if r.medusa_product_id:
            resolved.append(r.medusa_product_id)
            have.add(r.name)
    missing = [c for c in item_codes if c not in have]
    return resolved, missing


def _chunked(seq, n: int):
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i:i + n]
