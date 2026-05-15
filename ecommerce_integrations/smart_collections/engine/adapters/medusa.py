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

from ecommerce_integrations.medusa.connection import medusa_request, optional_session
from ecommerce_integrations.medusa.constants import API_CATEGORIES
from ecommerce_integrations.medusa.constants import MODULE_NAME as MEDUSA_MODULE
from ecommerce_integrations.smart_collections.constants import BACKEND_MEDUSA
from ecommerce_integrations.smart_collections.doctype.ecommerce_smart_collection.ecommerce_smart_collection import (
    slugify,
)
from ecommerce_integrations.smart_collections.engine.adapters.base import (
    AdapterError,
    CategoryAdapter,
    CategoryMatch,
    LiveCategoryState,
    register,
)

_LINK_BATCH_SIZE = 50


@register(BACKEND_MEDUSA)
class MedusaCategoryAdapter(CategoryAdapter):
    def upsert_category(self, collection, target) -> str:
        link_only = bool(getattr(target, "link_only", 0))

        # When the operator adopted an existing Medusa category with
        # link_only=1, leave the backend metadata as-is — only link
        # items. Mirrors the Shopware adapter's same-named guard.
        if target.external_id and link_only:
            return target.external_id

        handle = (collection.external_handle or "").strip() or collection.slug
        payload = {
            "name": collection.title,
            "handle": handle,
            "is_active": bool(collection.is_active),
        }
        if collection.description:
            payload["description"] = collection.description

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
                    # 404 means the cached external_id is stale — fall
                    # through to a create. Anything else is a real error.
                    if "404" not in str(e):
                        raise AdapterError(f"Medusa category update failed: {e}") from e

            try:
                resp = medusa_request(
                    session, base_url, "POST", API_CATEGORIES, json=payload,
                )
            except Exception as e:
                # ``handle already exists`` on create means an earlier run
                # created the category but failed before persisting its id
                # — recover by looking it up. Anything else is a real error.
                if "already exists" in _error_body(e).lower():
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

    def fetch_state(self, target) -> LiveCategoryState:
        if not target.external_id:
            return LiveCategoryState(exists=False)

        with optional_session() as (session, base_url):
            try:
                resp = medusa_request(
                    session, base_url, "GET",
                    f"{API_CATEGORIES}/{target.external_id}",
                    params={"fields": "id,name,handle,description,is_active"},
                )
            except Exception as e:
                if "404" in str(e):
                    return LiveCategoryState(exists=False)
                raise AdapterError(
                    f"Medusa category fetch failed: {e}",
                ) from e

            category = (resp or {}).get("product_category") or {}
            if not category:
                return LiveCategoryState(exists=False)

            # Pull linked products via the dedicated products endpoint.
            # Medusa v2 caps a single page at a few hundred, so paginate
            # offset/limit until exhausted (the same pattern the rest of
            # the Medusa module uses — see ``medusa_request_all``).
            linked: set[str] = set()
            total = 0
            offset = 0
            page_size = 200
            hard_cap = 5000
            while True:
                try:
                    page = medusa_request(
                        session, base_url, "GET",
                        f"{API_CATEGORIES}/{target.external_id}/products",
                        params={"limit": page_size, "offset": offset, "fields": "id"},
                    )
                except Exception as e:
                    if "404" in str(e):
                        page = {}
                    else:
                        raise AdapterError(
                            f"Medusa category-product fetch failed: {e}",
                        ) from e
                products = (page or {}).get("products") or []
                total = (page or {}).get("count", total) or total
                for p in products:
                    pid = p.get("id")
                    if pid:
                        linked.add(pid)
                if (
                    not products
                    or len(linked) >= hard_cap
                    or len(products) < page_size
                ):
                    break
                offset += page_size

        return LiveCategoryState(
            exists=True,
            external_id=target.external_id,
            name=category.get("name"),
            description=category.get("description"),
            active=bool(category.get("is_active", True)),
            sales_channel_ids=[],
            linked_product_ids=linked,
            product_count_total=int(total or len(linked)),
        )

    def find_matching_category(self, collection, target) -> list[CategoryMatch]:
        if target.external_id:
            return []
        title = (collection.title or "").strip()
        if not title:
            return []
        handle = (collection.external_handle or "").strip() or slugify(title)
        if not handle:
            return []

        with optional_session() as (session, base_url):
            try:
                resp = medusa_request(
                    session, base_url, "GET", API_CATEGORIES,
                    params={"handle": handle, "limit": 5, "fields": "id,name,handle"},
                )
            except Exception as e:
                if "404" in str(e):
                    return []
                raise AdapterError(
                    f"Medusa category search failed: {e}",
                ) from e

            categories = (resp or {}).get("product_categories") or []
            matches: list[CategoryMatch] = []
            for cat in categories:
                ext_id = cat.get("id")
                if not ext_id:
                    continue
                count = 0
                try:
                    count_resp = medusa_request(
                        session, base_url, "GET",
                        f"{API_CATEGORIES}/{ext_id}/products",
                        params={"limit": 1, "fields": "id"},
                    )
                    count = int((count_resp or {}).get("count") or 0)
                except Exception:
                    count = 0
                matches.append(
                    CategoryMatch(
                        external_id=ext_id,
                        name=cat.get("name") or title,
                        path=cat.get("handle") or handle,
                        # Medusa has no sales-channel-on-category concept,
                        # so this is always False for now — keeps the JS
                        # rendering code shared with Shopware.
                        has_target_sales_channel=False,
                        linked_product_count=count,
                    ),
                )
            return matches

    def _mutate_links(
        self, target, *, add: list[str] | tuple[str, ...], remove: list[str] | tuple[str, ...]
    ) -> list[str]:
        if not target.external_id:
            raise AdapterError(
                "link/unlink called before upsert_category — external_id is empty"
            )

        product_ids_add, missing = _items_to_medusa_product_ids(add)
        product_ids_remove, _ = _items_to_medusa_product_ids(remove)

        # Medusa v2 batch endpoint: ``POST {add: [...], remove: [...]}``.
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
    if not collection.parent_collection:
        return None
    external_id = frappe.db.get_value(
        "Ecommerce Smart Collection Target",
        {
            "parent": collection.parent_collection,
            "backend": target.backend,
            "sales_channel": target.sales_channel,
            "external_id": ["!=", ""],
        },
        "external_id",
    )
    return external_id or None


def _lookup_by_handle(session, base_url, handle: str) -> str | None:
    """Return the Medusa product-category id for ``handle`` if it exists."""
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


def _error_body(exc: Exception) -> str:
    """Best-effort extract of the response body from a ``requests.HTTPError``."""
    resp = getattr(exc, "response", None)
    if resp is None:
        return ""
    try:
        return resp.text or ""
    except Exception:
        return ""


def _items_to_medusa_product_ids(item_codes) -> tuple[list[str], list[str]]:
    """Map ERPNext item codes to Medusa product ids via ``tabEcommerce Item``.

    Variants in ERPNext have their own row but in Medusa they live as
    ``variants`` under the template product; only the template carries
    a Medusa product id. So if an item has no row of its own and is a
    variant, fall back to the template's row. The Smart Collection
    category then ends up linked to the template, which is what
    Medusa's storefront listings already do.

    Mirrors the Shopware adapter pattern at
    ``smart_collections/engine/adapters/shopware.py:_items_to_shopware_product_ids``
    so the two channels converge on the canonical mapping table
    (``integration='medusa'`` / ``integration='shopware6'``).

    Returns ``(resolved_product_ids, missing_item_codes)`` — the
    returned product ids are de-duplicated so a template with N
    variants in the same collection only links once.
    """
    if not item_codes:
        return [], []
    placeholders = ", ".join(["%s"] * len(item_codes))
    rows = frappe.db.sql(
        f"""
        SELECT i.name AS item_code,
               COALESCE(
                   NULLIF(self_ei.integration_item_code, ''),
                   parent_ei.integration_item_code
               ) AS product_id
        FROM `tabItem` i
        LEFT JOIN `tabEcommerce Item` self_ei
               ON self_ei.integration = %s
              AND self_ei.erpnext_item_code = i.name
        LEFT JOIN `tabEcommerce Item` parent_ei
               ON parent_ei.integration = %s
              AND parent_ei.erpnext_item_code = i.variant_of
        WHERE i.name IN ({placeholders})
        """,
        (MEDUSA_MODULE, MEDUSA_MODULE, *item_codes),
        as_dict=True,
    )
    seen: set[str] = set()
    resolved: list[str] = []
    have: set[str] = set()
    for r in rows:
        if r.product_id and r.product_id not in seen:
            resolved.append(r.product_id)
            seen.add(r.product_id)
        if r.product_id:
            have.add(r.item_code)
    missing = [c for c in item_codes if c not in have]
    return resolved, missing


def _chunked(seq, n: int):
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i:i + n]
