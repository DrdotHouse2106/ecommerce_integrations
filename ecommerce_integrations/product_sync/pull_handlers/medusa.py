"""Medusa -> ERPNext pull handlers.

Each function returns ``(succeeded, failed)`` counts so the Pull
orchestrator (``product_sync.pull_tasks.apply_pull``) can write them
onto the Sync Run audit row. Single-record failures increment
``failed`` and continue — one poisoned order shouldn't stop the rest
of the pull.

Strategy: paginated, watermark-driven GETs against the Medusa Admin
API. Orders go through ``GET /admin/orders`` filtered on
``updated_at[$gte]=<watermark>``, customers through ``GET /admin/customers``
the same way. Each hit is fed into the existing
``MedusaOrder.sync`` / ``MedusaCustomer.sync_to_erpnext`` so the
Pull Sync inherits all of the existing field-mapping and idempotency
logic without duplicating it here.

The inventory pull stays a stub: most operators treat ERPNext as the
source-of-truth for stock and the reverse direction only makes sense
for Medusa-as-POS setups. That lands in Phase-8.2.
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
    """Fetch Medusa orders updated since the watermark.

    Paginates ``GET /admin/orders?updated_at[$gte]=<watermark>``. For
    each order, hands off to ``MedusaOrder.sync(order_data)`` which is
    the same code path used by the webhook + the legacy scheduled
    sync — so customer-creation, item-resolution and Sales-Order
    mapping all stay consistent across triggers.

    Update-on-status-change is deferred to Phase-8.2; this first cut
    counts an already-existing Sales Order as a ``succeeded`` skip.
    """
    try:
        from ecommerce_integrations.medusa.connection import (
            get_medusa_session,
            medusa_request,
        )
        from ecommerce_integrations.medusa.constants import (
            API_ORDERS,
            ORDER_ID_FIELD,
        )
        from ecommerce_integrations.medusa.order.order_sync import MedusaOrder
    except ImportError as exc:
        raise NotImplementedError(
            f"Medusa order sync module not available: {exc}"
        ) from exc

    watermark = _watermark(sync_doc, "last_order_pulled_at")
    since = watermark.isoformat() if watermark else None

    # Keep the same association expansion the webhook handler uses so
    # ``MedusaOrder.sync`` can map without round-tripping back to the
    # API for ``customer`` / ``sales_channel`` / etc.
    fields = (
        "+items,+items.variant,+items.variant.product,"
        "+shipping_address,+billing_address,+shipping_methods,"
        "+transactions,+payment_collections,+payment_collections.payments,"
        "+payment_collections.payment_sessions,*customer,*customer.*,*sales_channel"
    )

    succeeded = 0
    failed = 0
    seen = 0
    offset = 0

    session, base_url = get_medusa_session()
    try:
        while seen < _MAX_PER_RUN:
            params: dict[str, Any] = {
                "limit": _PAGE_SIZE,
                "offset": offset,
                "order": "-updated_at",
                "fields": fields,
            }
            if since:
                # Medusa v2 filter syntax: ``updated_at[$gte]=<ISO>``.
                # TODO(phase-8.2): switch to ``$gt`` once we persist
                # watermarks at microsecond granularity to avoid the
                # one-second overlap on retry.
                params["updated_at[$gte]"] = since

            try:
                result = medusa_request(
                    session, base_url, "GET", API_ORDERS, params=params,
                )
            except Exception as exc:  # noqa: BLE001
                frappe.log_error(
                    title=f"Medusa pull_orders offset {offset} failed",
                    message=str(exc),
                )
                break

            orders: list[dict[str, Any]] = result.get("orders", []) or []
            if not orders:
                break

            # Bulk existence-check: one indexed SELECT per page.
            page_ids = [o.get("id") for o in orders if o.get("id")]
            already_synced = existing_record_set(
                "Sales Order", ORDER_ID_FIELD, page_ids,
            )

            for order_data in orders:
                seen += 1
                medusa_id = order_data.get("id")
                if not medusa_id:
                    continue
                if medusa_id in already_synced:
                    succeeded += 1
                    continue

                try:
                    order = MedusaOrder(medusa_id)
                    order.sync(order_data)
                    succeeded += 1
                except Exception as exc:  # noqa: BLE001
                    failed += 1
                    try:
                        frappe.db.rollback()
                    except Exception:
                        pass
                    frappe.log_error(
                        title=f"Medusa pull_orders failed for {medusa_id}",
                        message=str(exc),
                    )

            if len(orders) < _PAGE_SIZE:
                break
            offset += _PAGE_SIZE
    finally:
        try:
            session.close()
        except Exception:
            pass

    if seen >= _MAX_PER_RUN:
        frappe.logger("medusa").warning(
            f"Medusa pull_orders hit per-run cap ({_MAX_PER_RUN}); "
            f"lower lookback_hours on Pull Sync {sync_doc.name} and re-run."
        )

    return (succeeded, failed)


# ------------------------------------------------------------------ #
# Customers                                                          #
# ------------------------------------------------------------------ #


def pull_customers(sync_doc) -> tuple[int, int]:
    """Walk Medusa customers updated since the watermark.

    Paginates ``GET /admin/customers?updated_at[$gte]=<watermark>``.
    Each hit is fed into ``MedusaCustomer.sync_to_erpnext(data)`` which
    is idempotent — it links to an existing Customer if one matches by
    Medusa-id / contact-email / company.
    """
    try:
        from ecommerce_integrations.medusa.connection import (
            get_medusa_session,
            medusa_request,
        )
        from ecommerce_integrations.medusa.constants import (
            API_CUSTOMERS,
            CUSTOMER_ID_FIELD,
        )
        from ecommerce_integrations.medusa.customer import MedusaCustomer
    except ImportError as exc:
        raise NotImplementedError(
            f"Medusa customer sync module not available: {exc}"
        ) from exc

    watermark = _watermark(sync_doc, "last_customer_pulled_at")
    since = watermark.isoformat() if watermark else None

    succeeded = 0
    failed = 0
    seen = 0
    offset = 0

    session, base_url = get_medusa_session()
    try:
        while seen < _MAX_PER_RUN:
            params: dict[str, Any] = {
                "limit": _PAGE_SIZE,
                "offset": offset,
                "order": "-updated_at",
            }
            if since:
                params["updated_at[$gte]"] = since

            try:
                result = medusa_request(
                    session, base_url, "GET", API_CUSTOMERS, params=params,
                )
            except Exception as exc:  # noqa: BLE001
                frappe.log_error(
                    title=f"Medusa pull_customers offset {offset} failed",
                    message=str(exc),
                )
                break

            customers: list[dict[str, Any]] = result.get("customers", []) or []
            if not customers:
                break

            # Bulk existence-check: one indexed SELECT per page.
            page_ids = [c.get("id") for c in customers if c.get("id")]
            already_linked = existing_record_set(
                "Customer", CUSTOMER_ID_FIELD, page_ids,
            )

            for customer_data in customers:
                seen += 1
                medusa_id = customer_data.get("id")
                if not medusa_id:
                    continue
                if medusa_id in already_linked:
                    succeeded += 1
                    continue

                try:
                    MedusaCustomer(medusa_id).sync_to_erpnext(customer_data)
                    succeeded += 1
                except Exception as exc:  # noqa: BLE001
                    failed += 1
                    try:
                        frappe.db.rollback()
                    except Exception:
                        pass
                    frappe.log_error(
                        title=f"Medusa pull_customers failed for {medusa_id}",
                        message=str(exc),
                    )

            if len(customers) < _PAGE_SIZE:
                break
            offset += _PAGE_SIZE
    finally:
        try:
            session.close()
        except Exception:
            pass

    if seen >= _MAX_PER_RUN:
        frappe.logger("medusa").warning(
            f"Medusa pull_customers hit per-run cap ({_MAX_PER_RUN}); "
            f"lower lookback_hours on Pull Sync {sync_doc.name} and re-run."
        )

    return (succeeded, failed)


# ------------------------------------------------------------------ #
# Inventory                                                          #
# ------------------------------------------------------------------ #


def pull_inventory(sync_doc) -> tuple[int, int]:
    """Pull stock levels FROM Medusa.

    Phase-8.1: still a stub. ERPNext is the stock master in the
    standard setup; the reverse direction only makes sense for
    Medusa-as-POS and lands in Phase-8.2.
    """
    raise NotImplementedError(
        "Phase-8.2: reverse stock-sync from Medusa (POS-driven flows only)",
    )


# ------------------------------------------------------------------ #
# Helpers                                                            #
# ------------------------------------------------------------------ #


def _watermark(sync_doc, field: str):
    """Thin wrapper kept for callers — delegates to the shared helper."""
    return resolve_watermark(sync_doc, field)
