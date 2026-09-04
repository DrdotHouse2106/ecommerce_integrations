"""
Shopware 6 Payment Handler

Handles payment processing, invoice creation, and payment entries.
"""

from typing import Any

import frappe

from ecommerce_integrations.shopware6.constants import (
    PAYMENT_STATE_MAP,
    SETTING_DOCTYPE,
    STATUS_SYNC_ENQUEUE_TIMEOUT,
)
from ecommerce_integrations.shopware6.utils import create_shopware_log, get_logger


def create_sales_invoice(
    sales_order: str,
    transaction_data: dict[str, Any],
    setting
) -> str | None:
    """
    Create Sales Invoice from Shopware payment transaction.

    Also creates a Payment Entry if the order is marked as paid.
    This follows the same pattern as the Shopify integration.

    Args:
        sales_order: ERPNext Sales Order name
        transaction_data: Shopware transaction object
        setting: Shopware Setting document

    Returns:
        Sales Invoice name if created
    """
    try:
        # Check if SI already exists by transaction ID
        transaction_id = transaction_data.get("id")
        if transaction_id:
            existing = frappe.db.get_value(
                "Sales Invoice", {"shopware_transaction_id": transaction_id}, "name"
            )
            if existing:
                return existing

        # Robust check: Check if any submitted Sales Invoice is already linked to this Sales Order
        # This prevents duplicates if transaction IDs are inconsistent or webhooks fire concurrently
        existing_si = frappe.db.get_value(
            "Sales Invoice Item",
            {"sales_order": sales_order, "docstatus": ["!=", 2]},
            "parent"
        )
        if existing_si:
            return existing_si

        # Create Sales Invoice from Sales Order
        from ecommerce_integrations.utils.erpnext_compat import make_sales_invoice

        si = make_sales_invoice(sales_order)
        si.naming_series = setting.sales_invoice_series
        si.shopware_transaction_id = transaction_data.get("id")

        # Copy mode_of_payment from Sales Order to Invoice
        so_mode_of_payment = frappe.db.get_value("Sales Order", sales_order, "mode_of_payment")
        if so_mode_of_payment:
            si.mode_of_payment = so_mode_of_payment

        si.insert(ignore_permissions=True)
        si.submit()

        # Create Payment Entry for paid invoices (like Shopify does)
        # Check if transaction is paid
        state = transaction_data.get("stateMachineState", {})
        if state.get("technicalName") in ["paid", "completed"] and si.grand_total > 0:
            make_payment_entry_against_sales_invoice(si, sales_order, setting)

        return si.name

    except Exception:
        get_logger().error("Error occurred", persist=False)
        return None


def make_payment_entry_against_sales_invoice(
    sales_invoice,
    sales_order_name: str,
    setting
) -> str | None:
    """
    Create Payment Entry against a Sales Invoice.

    Uses the Mode of Payment mapped from the Shopware payment method.
    This follows the same pattern as the Shopify integration.

    Args:
        sales_invoice: Sales Invoice document
        sales_order_name: Sales Order name
        setting: Shopware Setting document

    Returns:
        Payment Entry name if created
    """
    from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

    try:
        # Operator opt-out (``skip_automatic_payment_entry`` on Shopware
        # Setting) for shops that reconcile payments through a separate
        # process (e.g. importing a PayPal account statement) — booking a
        # Payment Entry here as well would double it up. Applies regardless
        # of payment method; the Sales Invoice itself is still created.
        if getattr(setting, "skip_automatic_payment_entry", 0):
            return None

        # Get the Mode of Payment from the Sales Order
        mode_of_payment = frappe.db.get_value(
            "Sales Order", sales_order_name, "shopware_erpnext_mode_of_payment"
        )

        payment_entry = get_payment_entry(
            sales_invoice.doctype,
            sales_invoice.name,
            bank_account=setting.cash_bank_account
        )
        payment_entry.flags.ignore_mandatory = True
        payment_entry.reference_no = sales_invoice.name
        payment_entry.posting_date = sales_invoice.posting_date
        payment_entry.reference_date = sales_invoice.posting_date

        # Set Mode of Payment if we have a mapped value
        if mode_of_payment:
            payment_entry.mode_of_payment = mode_of_payment

        payment_entry.insert(ignore_permissions=True)
        payment_entry.submit()

        return payment_entry.name

    except Exception:
        get_logger().error("Error occurred", persist=False)
        return None


def verify_payment_status_from_shopware(order_id: str, sales_order_name: str):
    """
    Verify and update payment status from Shopware after order creation.

    This function handles the race condition where the payment webhook
    (order_transaction.state.changed) arrives before the order.placed webhook
    is fully processed. It fetches the current transaction status from Shopware
    and updates the Sales Order if needed.

    After verification, marks the order as ready (shopware_order_ready=1) if
    payment is not Failed/Cancelled. This triggers the confirmation email
    notification (which listens on Value Change of shopware_order_ready).

    Args:
        order_id: Shopware order ID
        sales_order_name: ERPNext Sales Order name
    """
    from ecommerce_integrations.shopware6.connection import get_shopware_client

    try:
        # Get current payment status from Sales Order
        current_status = frappe.db.get_value(
            "Sales Order", sales_order_name, "shopware_payment_status"
        )

        # If already marked as Paid, mark ready and return
        if current_status == "Paid":
            _mark_order_ready(sales_order_name)
            return

        # Fetch the latest transaction status from Shopware
        client = get_shopware_client()
        response = client.request_post("search/order-transaction", {
            "filter": [
                {"type": "equals", "field": "orderId", "value": order_id}
            ],
            "associations": {
                "stateMachineState": {}
            },
            "sort": [{"field": "createdAt", "order": "DESC"}],
            "limit": 1
        })

        transactions = response.data or []
        if not transactions:
            frappe.logger("shopware6").debug(
                f"No transactions found for order {order_id} during payment verification"
            )
            # No transactions found - mark ready based on current status
            if current_status not in ("Failed", "Cancelled"):
                _mark_order_ready(sales_order_name)
            return

        transaction = transactions[0]
        state = transaction.get("stateMachineState", {}) or {}
        state_name = state.get("technicalName", "open")

        # Map to ERPNext status
        new_status = PAYMENT_STATE_MAP.get(state_name, "Unpaid")

        # If status changed to Paid, update and trigger invoice/payment creation
        if new_status == "Paid" and current_status != "Paid":
            frappe.logger("shopware6").info(
                f"Payment verification: Updating {sales_order_name} status from {current_status} to {new_status}"
            )

            # Update payment status
            frappe.db.set_value(
                "Sales Order",
                sales_order_name,
                "shopware_payment_status",
                new_status
            )

            # Create Sales Invoice and Payment Entry if configured
            setting = frappe.get_doc(SETTING_DOCTYPE)

            if setting.sync_sales_invoice:
                # Check if invoice already exists
                existing_invoice = frappe.db.get_value(
                    "Sales Invoice Item",
                    {"sales_order": sales_order_name, "docstatus": 1},
                    "parent"
                )

                if not existing_invoice:
                    transaction_data = {
                        "id": transaction.get("id"),
                        "stateMachineState": state
                    }
                    create_sales_invoice(sales_order_name, transaction_data, setting)

            # Update Shopware transaction status to reflect payment
            from ecommerce_integrations.shopware6.status_sync import update_shopware_transaction_status
            frappe.enqueue(
                update_shopware_transaction_status,
                order_id=order_id,
                action="pay",
                queue="short",
                timeout=STATUS_SYNC_ENQUEUE_TIMEOUT,
            )

            frappe.db.commit()

            create_shopware_log(
                status="Success",
                method="verify_payment_status_from_shopware",
                message=f"Updated {sales_order_name} payment status to Paid (race condition fix)",
            )

        elif new_status != current_status:
            # Update to other status (e.g., Partly Paid, Failed)
            frappe.db.set_value(
                "Sales Order",
                sales_order_name,
                "shopware_payment_status",
                new_status
            )
            frappe.db.commit()

        # Mark order as ready for confirmation email if payment is OK
        # This is the verified status from Shopware (most up-to-date)
        if new_status not in ("Failed", "Cancelled"):
            _mark_order_ready(sales_order_name)

    except Exception:
        get_logger().error("Error occurred", persist=False)


def _mark_order_ready(sales_order_name: str):
    """Mark a Sales Order as ready for the Auftragsbestätigung email.

    Sets ``shopware_order_ready=1`` and ``ecommerce_order_confirmed=1``
    which trigger the "Ecommerce Order Acknowledgment" Notification.

    Payment-method-aware gating mirrors Shopware's own Flow Builder
    convention (see https://forum.shopware.com/t/95506):

    * Deferred-payment methods (Vorkasse, Rechnung, SEPA-Lastschrift)
      send the acknowledgment immediately on ``checkout.order.placed``,
      because the customer needs the bank details / invoice to actually
      pay. ``Unpaid`` is the expected, healthy state.
    * Instant-clearing methods (PayPal, Klarna, Karte, Stripe, Apple/
      Google Pay) wait until ``state_enter.order_transaction.state.paid``
      because an ``Unpaid`` PayPal/Klarna order may never settle —
      e.g. customer abandoned the PayPal flow, eCheck never clears,
      Klarna's fraud review rejects.

    The classification comes from
    ``Shopware Setting → Payment Method Mappings → is_deferred_payment``
    (operator-curated) with a name-based heuristic fallback so even an
    un-mapped method gets the right treatment.

    Always blocks on ``Failed`` / ``Cancelled`` regardless of method.
    """
    try:
        so = frappe.get_doc("Sales Order", sales_order_name)
        if so.get("shopware_order_ready"):
            return  # Already marked

        payment_status = so.get("shopware_payment_status") or ""
        payment_method = so.get("shopware_payment_method") or ""

        # Final-failure states never get an acknowledgment
        if payment_status in ("Failed", "Cancelled"):
            return

        # Money-has-arrived states always get one
        if payment_status in ("Paid", "Partly Paid", "Refunded"):
            pass  # fall through to mark-ready
        else:
            # Status is "Unpaid" (open/pending/in_progress/unconfirmed/…).
            # For instant-clearing methods we wait — the next
            # transaction.state.changed webhook will call us again when
            # the state transitions to Paid.
            if not _is_deferred_payment_method(payment_method):
                frappe.logger("shopware6").info(
                    f"Skipping acknowledgment for {sales_order_name}: "
                    f"payment_method='{payment_method}' is instant-clearing "
                    f"and status='{payment_status}' is not Paid yet. Will "
                    f"re-evaluate on next transaction.state.changed."
                )
                return

        so.shopware_order_ready = 1
        so.ecommerce_order_confirmed = 1
        so.flags.ignore_validate_update_after_submit = True
        so.save(ignore_permissions=True)
        frappe.db.commit()
    except Exception as e:
        frappe.logger("shopware6").error(
            f"Failed to mark order {sales_order_name} as ready: {e}"
        )


# Heuristic: methods whose technicalName contains any of these tokens
# are treated as "customer pays out-of-band" — i.e. ``Unpaid`` is a
# healthy state and the Auftragsbestätigung should be sent immediately.
# Used only when the operator has not classified the method explicitly
# on ``Shopware Setting → Payment Method Mappings``.
#
# Tokens are matched against the *flattened* form of the method name
# (lowercased, with separators removed) so spelling variants like
# ``s_e_p_a``, ``pre_payment``, ``cash-payment`` are caught alongside
# ``sepa`` / ``prepayment`` / ``cashpayment``.
_DEFERRED_PAYMENT_TOKENS: tuple[str, ...] = (
    "prepayment", "vorkasse",
    "invoice", "rechnung", "kaufaufrechnung",
    "pui",  # PayPal Pay Upon Invoice — settles only after invoice payment
    "cashondelivery", "cashpayment", "nachnahme", "cod",
    "sepa", "lastschrift", "directdebit", "debit",
)


def _normalize_method_name(name: str) -> str:
    """Lowercase + strip every separator/whitespace.

    Maps ``"S_E_P_A"``, ``"swag-paypal-paypal"``, ``"Cash Payment"`` all
    into a single canonical form so token containment checks fire
    reliably regardless of plugin author quirks.
    """
    flat = (name or "").lower()
    for ch in ("_", "-", " ", ".", "/"):
        flat = flat.replace(ch, "")
    return flat


def _is_deferred_payment_method(payment_method: str) -> bool:
    """Resolve whether ``payment_method`` waits for out-of-band payment.

    Two-tier resolution:

    1. **Explicit operator config** — look the method up on the
       ``Shopware Setting`` Payment-Method-Mappings child table and
       honour its ``is_deferred_payment`` flag. This is the source of
       truth when the operator has set it.
    2. **Heuristic fallback** — when no explicit mapping row matches
       (e.g. a new method that hasn't been configured yet), check the
       method's ``technicalName`` against ``_DEFERRED_PAYMENT_TOKENS``.
       Conservative default: anything unknown is treated as
       *instant-clearing*, so the worst-case for a misconfigured shop
       is a delayed (not erroneously sent) acknowledgment, which the
       operator can fix by ticking the box on the mapping row.

    Returns ``False`` on an empty ``payment_method`` so the conservative
    default kicks in even when the field hasn't been populated yet.
    """
    if not payment_method:
        return False

    normalized = _normalize_method_name(payment_method)

    # Tier 1: explicit operator configuration. Priority within tier 1:
    #
    #   (a) exact match — wins outright. Otherwise a generic ``cash``
    #       row would shadow a more-specific ``cash_payment`` *or*
    #       vice-versa depending on iteration order.
    #   (b) substring match, longest first — picks ``cash_payment``
    #       over ``cash`` when the incoming method is
    #       ``swag_cash_payment_handler``.
    try:
        setting = frappe.get_cached_doc(SETTING_DOCTYPE)
        substring_candidates: list[tuple[str, bool]] = []
        for row in (getattr(setting, "payment_method_mappings", None) or []):
            row_method = _normalize_method_name(row.shopware_method or "")
            if not row_method:
                continue
            row_deferred = bool(getattr(row, "is_deferred_payment", 0))
            if row_method == normalized:
                return row_deferred  # exact match — done
            if row_method in normalized or normalized in row_method:
                substring_candidates.append((row_method, row_deferred))
        if substring_candidates:
            substring_candidates.sort(key=lambda c: -len(c[0]))
            return substring_candidates[0][1]
    except Exception:
        # Setting unavailable (e.g. install/migrate context). Fall
        # through to the heuristic so we don't strand the email path.
        pass

    # Tier 2: name-based heuristic
    return any(token in normalized for token in _DEFERRED_PAYMENT_TOKENS)
