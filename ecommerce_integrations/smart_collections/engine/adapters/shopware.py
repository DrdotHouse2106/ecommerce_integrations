"""Shopware CategoryAdapter — Smart Collection ↔ Shopware category.

Uses the Shopware Admin API client (``Shopware6AdminAPIClientBase``).
Categories are created/updated under a configurable parent (the storefront
root navigation category). Channel visibility is set via the category's
``salesChannels`` association so ``All (30)`` etc. become real-world
visibility levels.

Item code → Shopware product id is read from
``Item.shopware_product_id`` (set by the Shopware product-export
pipeline). Items not yet synced to Shopware are skipped and returned to
the orchestrator as the unresolved set.
"""

import frappe

from ecommerce_integrations.shopware6.connection import temp_shopware_session
from ecommerce_integrations.smart_collections.engine.adapters.base import (
    AdapterError,
    CategoryAdapter,
    register,
)


_LINK_BATCH_SIZE = 50

_VISIBILITY_LEVEL = {
    "All (30)": 30,
    "Linked Only (20)": 20,
    "Search Only (10)": 10,
    "Hidden (0)": 0,
}


@register("Shopware")
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

    def _upsert_impl(self, client, collection, target) -> str:
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
        if (target.visibility or "All (30)") == "Hidden (0)":
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
    if not item_codes:
        return [], []
    rows = frappe.db.sql(
        """
        SELECT name, shopware_product_id
        FROM `tabItem`
        WHERE name IN ({placeholders})
        """.format(placeholders=", ".join(["%s"] * len(item_codes))),
        tuple(item_codes),
        as_dict=True,
    )
    resolved: list[str] = []
    have: set[str] = set()
    for r in rows:
        if r.shopware_product_id:
            resolved.append(r.shopware_product_id)
            have.add(r.name)
    missing = [c for c in item_codes if c not in have]
    return resolved, missing


def _chunked(seq, n: int):
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i:i + n]
