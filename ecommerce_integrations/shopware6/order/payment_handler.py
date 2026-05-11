"""
Shopware 6 Payment Handler

Handles payment processing, invoice creation, and payment entries.
"""

from typing import Any

import frappe

from ecommerce_integrations.shopware6.constants import SETTING_DOCTYPE, PAYMENT_STATE_MAP
from ecommerce_integrations.shopware6.utils import get_logger, create_shopware_log


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
        from erpnext.selling.doctype.sales_order.sales_order import make_sales_invoice

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

    except Exception as e:
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

    except Exception as e:
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

        transactions = response.get("data", [])
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
                timeout=60,
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

    except Exception as e:
        get_logger().error("Error occurred", persist=False)


def _mark_order_ready(sales_order_name: str):
    """
    Mark a Sales Order as ready for confirmation email.

    Uses doc.save() to trigger the Value Change notification on
    shopware_order_ready, which sends the "Bestellbestätigung" email.

    Args:
        sales_order_name: ERPNext Sales Order name
    """
    try:
        so = frappe.get_doc("Sales Order", sales_order_name)
        if so.get("shopware_order_ready"):
            return  # Already marked as ready

        so.shopware_order_ready = 1
        so.ecommerce_order_confirmed = 1
        so.flags.ignore_validate_update_after_submit = True
        so.save(ignore_permissions=True)
        frappe.db.commit()
    except Exception as e:
        frappe.logger("shopware6").error(
            f"Failed to mark order {sales_order_name} as ready: {e}"
        )
