"""Shopware -> ERPNext pull handlers.

Each function returns ``(succeeded, failed)`` counts so the Pull
orchestrator (``product_sync.pull_tasks.apply_pull``) can write them
onto the Sync Run audit row. Counts are best-effort: when a single
order/customer fails we increment ``failed`` and continue with the
next one so a poisoned record can't stop a whole pull.

Strategy: paginated, watermark-driven ``POST /api/search/order`` and
``POST /api/search/customer`` against the Shopware 6 Admin API. We
reuse the existing ``shopware6`` module wherever possible —
``create_sales_order`` for the order side and
``create_customer_from_shopware_data`` for the customer side — so the
Pull Sync inherits all of the existing field-mapping, address-handling
and idempotency logic without duplicating it here.

The inventory pull stays a stub: most operators treat ERPNext as the
source-of-truth for stock and the reverse direction only makes sense
for POS-driven flows. That lands in Phase-8.2 alongside the
update-on-status-change path.
"""

from __future__ import annotations

from typing import Any

import frappe

from ecommerce_integrations.product_sync.pull_handlers._common import (
    MAX_PER_RUN as _MAX_PER_RUN,
    PAGE_SIZE as _PAGE_SIZE,
    existing_record_set,
    resolve_watermark,
)


# ------------------------------------------------------------------ #
# Orders                                                             #
# ------------------------------------------------------------------ #


def pull_orders(sync_doc) -> tuple[int, int]:
    """Fetch Shopware orders updated since the watermark.

    Walks ``POST /api/search/order`` paginated, filtered on
    ``updatedAt >= watermark``. For each hit we delegate to the
    existing ``create_sales_order(order_data)`` helper from
    ``shopware6.order.order_sync`` — the same code path the webhook
    and the legacy scheduled sync use, so behaviour stays consistent.

    Already-synced orders (existing ``Sales Order`` with the same
    ``shopware_order_id``) are counted as ``succeeded`` and skipped.
    Update-on-status-change is deferred to Phase-8.2.
    """
    try:
        from lib_shopware6_api_base import Criteria, RangeFilter

        from ecommerce_integrations.shopware6.connection import get_shopware_client
        from ecommerce_integrations.shopware6.order.order_sync import create_sales_order
        from ecommerce_integrations.shopware6.order.scheduled_sync import (
            build_order_criteria,
        )
    except ImportError as exc:
        raise NotImplementedError(
            f"Shopware order sync module not available: {exc}"
        ) from exc

    watermark = _watermark(sync_doc, "last_order_pulled_at")
    since_iso = _as_shopware_iso(watermark)

    client = get_shopware_client()

    succeeded = 0
    failed = 0
    seen = 0
    page = 1

    while seen < _MAX_PER_RUN:
        # Reuse ``build_order_criteria`` for the association graph,
        # then bolt on the incremental filter + pagination. Shopware's
        # filter syntax: any of {gt,gte,lt,lte} can be combined inside
        # one ``RangeFilter``. We use ``gte`` because the watermark
        # itself is the cursor — overlapping by one second on retry
        # is fine, the existing-order guard de-dupes.
        #
        # Cursor field is ``orderDateTime`` (the business order date)
        # rather than ``updatedAt``. Roughly half of Shopware orders
        # carry ``updatedAt=NULL`` because they were never touched
        # after creation, and a ``RangeFilter`` with ``gte`` excludes
        # NULL rows — so a watermarked pull on ``updatedAt`` silently
        # skips every untouched order. ``orderDateTime`` is always
        # populated and matches the "pull new orders since X"
        # semantics the operator actually wants.
        # TODO(phase-8.2): switch to ``gt`` once we persist watermarks
        # at microsecond granularity to avoid the one-second overlap,
        # and add a parallel ``updatedAt`` cursor for update-on-status
        # so post-creation state changes flow without webhook.
        criteria = build_order_criteria(limit=_PAGE_SIZE)
        criteria.page = page  # type: ignore[attr-defined]
        criteria.filter.append(
            RangeFilter(field="orderDateTime", parameters={"gte": since_iso}),
        )

        try:
            response = client.request_post("search/order", criteria)
        except Exception as exc:  # noqa: BLE001
            frappe.log_error(
                title=f"Shopware pull_orders page {page} failed",
                message=str(exc),
            )
            break

        orders: list[dict[str, Any]] = response.data or []
        if not orders:
            break

        # Bulk-check existence: one indexed SELECT for the whole page
        # instead of one ``frappe.db.get_value`` per order.
        page_ids = [o.get("id") for o in orders if o.get("id")]
        already_synced = existing_record_set(
            "Sales Order", "shopware_order_id", page_ids,
        )

        for order in orders:
            seen += 1
            order_id = order.get("id")
            if not order_id:
                continue
            if order_id in already_synced:
                succeeded += 1
                continue

            # Skip cancelled orders — mirrors the legacy scheduled sync.
            order_state = (order.get("stateMachineState") or {}).get(
                "technicalName", "",
            )
            if order_state == "cancelled":
                continue

            try:
                create_sales_order(order)
                succeeded += 1
            except Exception as exc:  # noqa: BLE001
                failed += 1
                # Roll back the failed transaction so the next order
                # in the loop doesn't inherit half-applied state /
                # held locks.
                try:
                    frappe.db.rollback()
                except Exception:
                    pass
                frappe.log_error(
                    title=f"Shopware pull_orders failed for {order_id}",
                    message=str(exc),
                )

        if len(orders) < _PAGE_SIZE:
            break
        page += 1

    if seen >= _MAX_PER_RUN:
        frappe.logger("shopware6").warning(
            f"Shopware pull_orders hit per-run cap ({_MAX_PER_RUN}); "
            f"lower lookback_hours on Pull Sync {sync_doc.name} and re-run."
        )

    return (succeeded, failed)


# ------------------------------------------------------------------ #
# Customers                                                          #
# ------------------------------------------------------------------ #


def pull_customers(sync_doc) -> tuple[int, int]:
    """Walk Shopware customers updated since the watermark.

    Uses ``POST /api/search/customer`` with a ``RangeFilter`` on
    ``updatedAt``. Each hit is fed into ``create_customer_from_shopware_data``
    so customer-matching, VAT-linking and address handling are reused
    from the existing module.
    """
    try:
        from lib_shopware6_api_base import RangeFilter

        from ecommerce_integrations.shopware6.connection import get_shopware_client
        from ecommerce_integrations.shopware6.customer.sync import (
            _build_customer_criteria,
            create_customer_from_shopware_data,
        )
    except ImportError as exc:
        raise NotImplementedError(
            f"Shopware customer sync module not available: {exc}"
        ) from exc

    watermark = _watermark(sync_doc, "last_customer_pulled_at")
    since_iso = _as_shopware_iso(watermark)

    client = get_shopware_client()

    succeeded = 0
    failed = 0
    seen = 0
    page = 1

    while seen < _MAX_PER_RUN:
        criteria = _build_customer_criteria(limit=_PAGE_SIZE)
        criteria.page = page  # type: ignore[attr-defined]
        criteria.filter.append(
            RangeFilter(field="updatedAt", parameters={"gte": since_iso}),
        )

        try:
            response = client.request_post("search/customer", criteria)
        except Exception as exc:  # noqa: BLE001
            frappe.log_error(
                title=f"Shopware pull_customers page {page} failed",
                message=str(exc),
            )
            break

        customers: list[dict[str, Any]] = response.data or []
        if not customers:
            break

        # Bulk-check existence: one indexed SELECT per page.
        # ``create_customer_from_shopware_data`` is itself idempotent,
        # but we still skip API work for already-linked customers so
        # ``succeeded`` reflects "row reconciled" not "API call made".
        page_ids = [c.get("id") for c in customers if c.get("id")]
        already_linked = existing_record_set(
            "Customer", "shopware_customer_id", page_ids,
        )

        for customer_data in customers:
            seen += 1
            customer_id = customer_data.get("id")
            if not customer_id:
                continue
            if customer_id in already_linked:
                succeeded += 1
                continue

            try:
                create_customer_from_shopware_data(customer_data)
                succeeded += 1
            except Exception as exc:  # noqa: BLE001
                failed += 1
                try:
                    frappe.db.rollback()
                except Exception:
                    pass
                frappe.log_error(
                    title=f"Shopware pull_customers failed for {customer_id}",
                    message=str(exc),
                )

        if len(customers) < _PAGE_SIZE:
            break
        page += 1

    if seen >= _MAX_PER_RUN:
        frappe.logger("shopware6").warning(
            f"Shopware pull_customers hit per-run cap ({_MAX_PER_RUN}); "
            f"lower lookback_hours on Pull Sync {sync_doc.name} and re-run."
        )

    return (succeeded, failed)


# ------------------------------------------------------------------ #
# Inventory                                                          #
# ------------------------------------------------------------------ #


def pull_inventory(sync_doc) -> tuple[int, int]:
    """Pull stock levels FROM Shopware.

    Phase-8.1: still a stub. ERPNext is the stock master for almost
    every operator we ship to; the reverse direction is only useful
    for POS-driven setups and lands in Phase-8.2 together with the
    push-side reverse-stock-sync wiring.
    """
    raise NotImplementedError(
        "Phase-8.2: reverse stock-sync from Shopware (POS-driven flows only)",
    )


# ------------------------------------------------------------------ #
# Helpers                                                            #
# ------------------------------------------------------------------ #


def _watermark(sync_doc, field: str):
    """Thin wrapper kept for callers — delegates to the shared helper."""
    return resolve_watermark(sync_doc, field)


def _as_shopware_iso(dt) -> str:
    """Format a ``datetime`` the way the Shopware Admin API expects.

    Shopware accepts ISO-8601 with millisecond precision and a UTC
    suffix; the existing scheduled sync uses the same shape
    (see ``shopware6.order.scheduled_sync.build_order_criteria``).
    """
    try:
        return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    except Exception:
        # Fallback: ``datetime.isoformat`` — Shopware accepts plain ISO
        # too, just with less interop with its own clock.
        return str(dt)
