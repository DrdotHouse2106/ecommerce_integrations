"""Shopware CategoryAdapter — Smart Collection ↔ Shopware category.

Uses the Shopware Admin API client (``Shopware6AdminAPIClientBase``).
Categories are created/updated under a configurable parent (the storefront
root navigation category). Channel visibility is set via the category's
``salesChannels`` association so ``All (30)`` etc. become real-world
visibility levels.

Item code → Shopware product id is read from ``tabEcommerce Item`` —
the canonical cross-integration mapping table written by the Shopware
product-export pipeline (``integration='shopware6'``,
``erpnext_item_code`` ↔ ``integration_item_code``). Items not yet
exported to Shopware are returned to the orchestrator as the unresolved
set so they can be retried after the next product export.
"""

import frappe

from ecommerce_integrations.shopware6.connection import temp_shopware_session
from ecommerce_integrations.shopware6.constants import MODULE_NAME as SHOPWARE_MODULE
from ecommerce_integrations.smart_collections.constants import BACKEND_SHOPWARE
from ecommerce_integrations.smart_collections.engine.adapters.base import (
    AdapterError,
    CategoryAdapter,
    CategoryMatch,
    LiveCategoryState,
    register,
)


from ecommerce_integrations.smart_collections.constants import (
    VISIBILITY_DEFAULT,
    VISIBILITY_LEVELS,
)


_LINK_BATCH_SIZE = 50


@register(BACKEND_SHOPWARE)
class ShopwareCategoryAdapter(CategoryAdapter):
    def upsert_category(self, collection, target) -> str:
        return _with_client(self._upsert_impl, collection, target)

    def link_items(self, target, item_codes: list[str]) -> list[str]:
        return _with_client(self._link_impl, target, item_codes)

    def unlink_items(self, target, item_codes: list[str]) -> None:
        _with_client(self._unlink_impl, target, item_codes)

    def delete_category(self, target) -> None:
        if not target.external_id:
            return
        _with_client(self._delete_impl, target)

    def fetch_state(self, target) -> LiveCategoryState:
        if not target.external_id:
            return LiveCategoryState(exists=False)
        return _with_client(self._fetch_state_impl, target)

    def find_matching_category(self, collection, target) -> list[CategoryMatch]:
        if target.external_id:
            return []
        return _with_client(self._find_matching_impl, collection, target)

    def _upsert_impl(self, client, collection, target) -> str:
        link_only = bool(getattr(target, "link_only", 0))

        # When the operator adopted an existing backend category with
        # link_only=1, the intent is "use this category as-is — just
        # link my items". Pushing the collection's title/description as
        # a PATCH would rename a manually-curated Shopware category,
        # which is exactly the destruction operators wanted to avoid.
        # Skip the metadata PATCH; only ensure the sales-channel
        # association is set so the category remains visible on the
        # operator's target storefront.
        if target.external_id and link_only:
            self._set_sales_channel_assignments(client, target)
            return target.external_id

        payload = {
            "name": collection.title,
            "active": bool(collection.is_active),
            "type": "page",
            "displayNestedProducts": True,
            "visible": True,
            "translations": {"en-GB": {"name": collection.title}},
        }
        if collection.description:
            payload["description"] = collection.description

        if target.external_id:
            try:
                client.request_patch(
                    f"category/{target.external_id}", payload=payload,
                )
                self._set_sales_channel_assignments(client, target)
                return target.external_id
            except Exception as e:
                if "404" not in str(e):
                    raise AdapterError(f"Shopware category update failed: {e}") from e
                # Fall through to create when the recorded id no longer exists.

        try:
            response = client.request_post("category", payload=payload)
        except Exception as e:
            raise AdapterError(f"Shopware category create failed: {e}") from e

        external_id = (response or {}).get("data", {}).get("id")
        if not external_id:
            raise AdapterError(
                f"Shopware category create returned no id: {response!r}"
            )
        target.external_id = external_id
        self._set_sales_channel_assignments(client, target)
        return external_id

    def _set_sales_channel_assignments(self, client, target) -> None:
        # The category's ``salesChannels`` association is the visibility
        # gate on the storefront. Linking the channel exposes the
        # category; visibility level is implicit (Shopware doesn't have
        # a per-category visibility level — we encode that on the items
        # via the existing product visibility custom fields).
        if not (target.external_id and target.sales_channel):
            return
        if VISIBILITY_LEVELS.get(target.visibility or VISIBILITY_DEFAULT, 30) == 0:
            try:
                client.request_delete(
                    f"category/{target.external_id}/sales-channels/"
                    f"{target.sales_channel}",
                )
            except Exception:
                # Idempotent: missing assignment is fine.
                pass
            return
        try:
            client.request_post(
                f"category/{target.external_id}/sales-channels",
                payload={"id": target.sales_channel},
            )
        except Exception as e:
            if "409" in str(e):
                # Already assigned — fine.
                return
            raise AdapterError(
                f"Shopware sales-channel assign failed: {e}"
            ) from e

    def _link_impl(self, client, target, item_codes: list[str]) -> list[str]:
        if not target.external_id:
            raise AdapterError(
                "link called before upsert_category — external_id is empty"
            )
        product_ids, missing = _items_to_shopware_product_ids(item_codes)
        for batch in _chunked(product_ids, _LINK_BATCH_SIZE):
            payload = {"productIds": batch}
            try:
                client.request_post(
                    f"_action/category/{target.external_id}/products",
                    payload=payload,
                )
            except Exception as e:
                raise AdapterError(f"Shopware link batch failed: {e}") from e
        return missing

    def _unlink_impl(self, client, target, item_codes: list[str]) -> None:
        if not target.external_id:
            raise AdapterError(
                "unlink called before upsert_category — external_id is empty"
            )
        product_ids, _ = _items_to_shopware_product_ids(item_codes)
        for product_id in product_ids:
            try:
                client.request_delete(
                    f"category/{target.external_id}/products/{product_id}",
                )
            except Exception as e:
                if "404" in str(e):
                    continue
                raise AdapterError(f"Shopware unlink failed: {e}") from e

    def _fetch_state_impl(self, client, target) -> LiveCategoryState:
        try:
            resp = client.request_get(f"category/{target.external_id}")
        except Exception as e:
            if "404" in str(e):
                return LiveCategoryState(exists=False)
            raise AdapterError(f"Shopware category fetch failed: {e}") from e

        data = (resp or {}).get("data") or {}
        attrs = data.get("attributes") or data
        name = attrs.get("name")
        description = attrs.get("description")
        active = bool(attrs.get("active", True))

        sales_channel_ids: list[str] = []
        try:
            sc_resp = client.request_get(
                f"category/{target.external_id}/sales-channels",
            )
            for row in (sc_resp or {}).get("data") or []:
                sc_id = row.get("id") or (row.get("attributes") or {}).get("id")
                if sc_id:
                    sales_channel_ids.append(sc_id)
        except Exception:
            # Listing sales-channels is best-effort — a missing association
            # is the same outcome as an empty list for the preview, and
            # the operator can still inspect the live state from the dialog.
            pass

        linked_product_ids, total = _fetch_linked_product_ids(
            client, target.external_id,
        )

        return LiveCategoryState(
            exists=True,
            external_id=target.external_id,
            name=name,
            description=description,
            active=active,
            sales_channel_ids=sales_channel_ids,
            linked_product_ids=linked_product_ids,
            product_count_total=total,
        )

    def _find_matching_impl(
        self, client, collection, target,
    ) -> list[CategoryMatch]:
        candidates = _candidate_names(collection)
        if not candidates:
            return []

        parent_id: str | None = None
        if target.sales_channel:
            try:
                sc_resp = client.request_get(
                    f"sales-channel/{target.sales_channel}",
                )
                data = (sc_resp or {}).get("data") or {}
                attrs = data.get("attributes") or data
                parent_id = attrs.get("navigationCategoryId")
            except Exception:
                # Sales-channel lookup is advisory — without it we search
                # across all categories and surface the matches anyway.
                parent_id = None

        matches: list[CategoryMatch] = []
        seen_ids: set[str] = set()

        # Search globally across the Shopware category tree for each
        # candidate. We deliberately do NOT filter by parentId —
        # operators often have multiple categories with the same name
        # in different navigation trees (per sales channel, archive,
        # staging duplicates, etc.) and surfacing all of them lets the
        # operator pick the right one. ``has_target_sales_channel``
        # still flags whichever match is wired up to the target
        # sales channel, so the JS can sort those to the top.
        for cand in candidates:
            criteria = {
                "filter": [
                    {"type": "equals", "field": "name", "value": cand},
                ],
                "limit": 50,
            }
            try:
                resp = client.request_post("search/category", payload=criteria)
            except Exception as e:
                raise AdapterError(
                    f"Shopware category search failed: {e}",
                ) from e

            for row in (resp or {}).get("data") or []:
                attrs = row.get("attributes") or row
                ext_id = row.get("id") or attrs.get("id")
                if not ext_id or ext_id in seen_ids:
                    continue
                seen_ids.add(ext_id)
                cat_name = attrs.get("name") or cand
                breadcrumb = attrs.get("breadcrumb") or [cat_name]
                path = " / ".join(str(p) for p in breadcrumb)
                linked = _fetch_category_product_count(client, ext_id)

                has_channel = False
                if target.sales_channel:
                    try:
                        sc_resp = client.request_get(
                            f"category/{ext_id}/sales-channels",
                        )
                        for sc in (sc_resp or {}).get("data") or []:
                            sc_id = sc.get("id") or (sc.get("attributes") or {}).get("id")
                            if sc_id == target.sales_channel:
                                has_channel = True
                                break
                    except Exception:
                        has_channel = False

                # Categories under the target sales channel's
                # navigation root are usually the intended targets
                # (operators rarely want to adopt a category outside
                # their channel tree). Use the parent_id we fetched
                # earlier to detect this. Direct children of the nav
                # root are clearly in-channel; descendants further down
                # are likely in-channel too if the breadcrumb starts
                # with the channel's root.
                in_channel_tree = False
                if parent_id and breadcrumb:
                    # Walk the chain: a category is "in-channel" if its
                    # breadcrumb's root id chain reaches parent_id.
                    # Shopware's breadcrumb is a list of names not ids,
                    # so we accept the looser signal "category has the
                    # sales-channel association set".
                    in_channel_tree = has_channel

                matches.append(
                    CategoryMatch(
                        external_id=ext_id,
                        name=cat_name,
                        path=path,
                        has_target_sales_channel=has_channel or in_channel_tree,
                        linked_product_count=linked,
                    ),
                )

        # Sort: matches with the target sales-channel association first,
        # then by linked product count desc (operators usually want the
        # category that's already populated, not an empty duplicate).
        matches.sort(
            key=lambda m: (not m.has_target_sales_channel, -m.linked_product_count),
        )
        return matches

    def _delete_impl(self, client, target) -> None:
        try:
            client.request_delete(f"category/{target.external_id}")
        except Exception as e:
            if "404" in str(e):
                return
            raise AdapterError(f"Shopware category delete failed: {e}") from e


def _with_client(fn, *args, **kwargs):
    """Run ``fn`` inside a Shopware admin session.

    The decorator ``temp_shopware_session`` injects the client as the
    first arg; we forward the rest.
    """
    @temp_shopware_session
    def runner(client):
        return fn(client, *args, **kwargs)

    return runner()


def _items_to_shopware_product_ids(item_codes) -> tuple[list[str], list[str]]:
    """Resolve ERPNext item codes to Shopware product UUIDs via
    ``tabEcommerce Item`` (``integration='shopware6'``). Returns
    ``(resolved_product_ids, missing_item_codes)`` with product ids
    de-duplicated — two ERPNext rows pointing at the same Shopware
    product (theoretically a parent/variant collision) link the
    category to that product only once.
    """
    if not item_codes:
        return [], []
    placeholders = ", ".join(["%s"] * len(item_codes))
    rows = frappe.db.sql(
        f"""
        SELECT erpnext_item_code AS item_code,
               integration_item_code AS product_id
        FROM `tabEcommerce Item`
        WHERE integration = %s
          AND erpnext_item_code IN ({placeholders})
          AND integration_item_code IS NOT NULL
          AND integration_item_code != ''
        """,
        (SHOPWARE_MODULE, *item_codes),
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


_PREVIEW_PRODUCT_PAGE = 500
_PREVIEW_PRODUCT_HARD_CAP = 5000


def _fetch_linked_product_ids(client, category_id: str) -> tuple[set[str], int]:
    """Return ``(set_of_product_ids, total_count)`` for ``category_id``.

    Paginates ``POST /search/product`` filtered by
    ``categories.id = category_id``. The returned set is capped at
    :data:`_PREVIEW_PRODUCT_HARD_CAP` to keep the dry-run preview fast
    on huge categories; ``total`` always reflects the real category
    size so the dialog can warn the operator.
    """
    product_ids: set[str] = set()
    total = 0
    page = 1
    while True:
        criteria = {
            "page": page,
            "limit": _PREVIEW_PRODUCT_PAGE,
            "filter": [
                {"type": "equals", "field": "categories.id", "value": category_id},
            ],
            "total-count-mode": 1,
            "includes": {"product": ["id"]},
        }
        try:
            resp = client.request_post("search/product", payload=criteria)
        except Exception as e:
            raise AdapterError(
                f"Shopware product search failed: {e}",
            ) from e
        data = (resp or {}).get("data") or []
        total = (resp or {}).get("total", total) or total
        for row in data:
            pid = row.get("id") or (row.get("attributes") or {}).get("id")
            if pid:
                product_ids.add(pid)
        if (
            not data
            or len(product_ids) >= _PREVIEW_PRODUCT_HARD_CAP
            or len(data) < _PREVIEW_PRODUCT_PAGE
        ):
            break
        page += 1
    return product_ids, int(total)


def _fetch_category_product_count(client, category_id: str) -> int:
    """One ``POST /search/product`` with ``limit=1`` to read the total."""
    criteria = {
        "limit": 1,
        "filter": [
            {"type": "equals", "field": "categories.id", "value": category_id},
        ],
        "total-count-mode": 1,
        "includes": {"product": ["id"]},
    }
    try:
        resp = client.request_post("search/product", payload=criteria)
    except Exception:
        return 0
    return int((resp or {}).get("total") or 0)


def _candidate_names(collection) -> list[str]:
    """Build an ordered list of category names to search Shopware for.

    Order is most-specific-first:
      1. ``collection.title`` (e.g. "Produkte (Test GmbH)")
      2. ``collection.title`` without a trailing parenthesized brand tag
         (e.g. "Produkte")
      3. Values from ``Item Group`` rules — if the collection's rules
         filter by Item Group (``rule_type='Item Group'``, operator in
         ``equals``/``descends_from``), use the rule's value (e.g.
         "Produkte" from a rule ``Item Group descends_from Produkte``).
         These match the legacy convention where Shopware categories
         were generated 1:1 from ERPNext Item Groups.
    Duplicates removed while preserving order.
    """
    import re

    seen: set[str] = set()
    out: list[str] = []

    def _add(name):
        if not name:
            return
        n = str(name).strip()
        if n and n not in seen:
            seen.add(n)
            out.append(n)

    _add(getattr(collection, "title", None))

    title = (getattr(collection, "title", None) or "").strip()
    if title:
        # Strip trailing "(...)" — common brand-tag convention.
        stripped = re.sub(r"\s*\([^)]*\)\s*$", "", title).strip()
        if stripped:
            _add(stripped)

    # ``external_handle`` is a user-facing override that wins when set.
    _add(getattr(collection, "external_handle", None))

    # Pull Item Group values from the rules.
    for rule in getattr(collection, "rules", None) or []:
        rule_type = getattr(rule, "rule_type", None)
        op = (getattr(rule, "operator", "") or "").lower()
        value = getattr(rule, "value", None)
        if rule_type == "Item Group" and op in ("equals", "descends_from", "="):
            _add(value)

    return out
