"""
Status Sync: ERPNext -> Shopware

Syncs document status changes from ERPNext to Shopware:
- Delivery Note submit -> Shopware delivery status "shipped"
- Sales Invoice submit -> Shopware transaction status "paid"
- Sales Order cancel -> Shopware order status "cancelled"
"""

import frappe
from frappe import _
from typing import Optional

from ecommerce_integrations.shopware6.connection import temp_shopware_session
from ecommerce_integrations.shopware6.constants import (
    SETTING_DOCTYPE,
    ORDER_ID_FIELD,
    DELIVERY_ID_FIELD,
    TRANSACTION_ID_FIELD,
)
from ecommerce_integrations.shopware6.utils import create_shopware_log


def is_shopware_enabled() -> bool:
    """Check if Shopware integration is enabled."""
    try:
        setting = frappe.get_cached_doc(SETTING_DOCTYPE)
        return setting.is_enabled()
    except Exception:
        return False


@temp_shopware_session
def update_shopware_order_status(client, order_id: str, action: str) -> bool:
    """
    Update Shopware order state machine.

    Args:
        client: Shopware API client
        order_id: Shopware order ID
        action: State machine action (e.g., 'process', 'complete', 'cancel')

    Returns:
        bool: True if successful
    """
    try:
        # Shopware uses state machine transitions
        # POST /_action/state-machine/order/{orderId}/state/{action}
        endpoint = f"_action/state-machine/order/{order_id}/state/{action}"
        client.request_post(endpoint, {})
        return True
    except Exception as e:
        frappe.log_error(
            f"Failed to update Shopware order {order_id} status to {action}: {e}",
            "Shopware Status Sync Error"
        )
        return False


@temp_shopware_session
def update_shopware_delivery_status(client, order_id: str, action: str) -> bool:
    """
    Update Shopware delivery state machine.

    Args:
        client: Shopware API client
        order_id: Shopware order ID
        action: State machine action (e.g., 'ship', 'ship_partially', 'retour')

    Returns:
        bool: True if successful
    """
    try:
        # First get the delivery ID for this order
        response = client.request_post("search/order-delivery", {
            "filter": [{"type": "equals", "field": "orderId", "value": order_id}],
            "limit": 1
        })

        deliveries = response.get("data", [])
        if not deliveries:
            frappe.log_error(
                f"No delivery found for Shopware order {order_id}",
                "Shopware Status Sync Error"
            )
            return False

        delivery_id = deliveries[0].get("id")

        # Update delivery state
        endpoint = f"_action/state-machine/order_delivery/{delivery_id}/state/{action}"
        client.request_post(endpoint, {})
        return True
    except Exception as e:
        frappe.log_error(
            f"Failed to update Shopware delivery for order {order_id} to {action}: {e}",
            "Shopware Status Sync Error"
        )
        return False


@temp_shopware_session
def update_shopware_transaction_status(client, order_id: str, action: str) -> bool:
    """
    Update Shopware transaction (payment) state machine.

    Args:
        client: Shopware API client
        order_id: Shopware order ID
        action: State machine action (e.g., 'pay', 'pay_partially', 'refund', 'cancel')

    Returns:
        bool: True if successful
    """
    try:
        # First get the transaction ID for this order
        response = client.request_post("search/order-transaction", {
            "filter": [{"type": "equals", "field": "orderId", "value": order_id}],
            "limit": 1
        })

        transactions = response.get("data", [])
        if not transactions:
            frappe.log_error(
                f"No transaction found for Shopware order {order_id}",
                "Shopware Status Sync Error"
            )
            return False

        transaction_id = transactions[0].get("id")

        # Update transaction state
        endpoint = f"_action/state-machine/order_transaction/{transaction_id}/state/{action}"
        client.request_post(endpoint, {})
        return True
    except Exception as e:
        frappe.log_error(
            f"Failed to update Shopware transaction for order {order_id} to {action}: {e}",
            "Shopware Status Sync Error"
        )
        return False


def on_delivery_note_submit(doc, method=None):
    """
    Hook: When Delivery Note is submitted, update Shopware delivery status to 'shipped'.
    """
    if not is_shopware_enabled():
        return

    # Check if this Delivery Note is linked to a Shopware order
    # Try to get the Shopware order ID from the linked Sales Order
    if not doc.items:
        return

    sales_order_name = doc.items[0].against_sales_order if doc.items else None
    if not sales_order_name:
        return

    shopware_order_id = frappe.db.get_value("Sales Order", sales_order_name, ORDER_ID_FIELD)
    if not shopware_order_id:
        return  # Not a Shopware order

    # Update Shopware delivery status
    frappe.enqueue(
        update_shopware_delivery_status,
        order_id=shopware_order_id,
        action="ship",
        queue="short",
        timeout=60
    )

    create_shopware_log(
        status="Queued",
        method="on_delivery_note_submit",
        message=f"Delivery status update queued for Shopware order {shopware_order_id}"
    )


def on_delivery_note_cancel(doc, method=None):
    """
    Hook: When Delivery Note is cancelled, update Shopware delivery status back to 'open'.
    """
    if not is_shopware_enabled():
        return

    sales_order_name = doc.items[0].against_sales_order if doc.items else None
    if not sales_order_name:
        return

    shopware_order_id = frappe.db.get_value("Sales Order", sales_order_name, ORDER_ID_FIELD)
    if not shopware_order_id:
        return

    # Reopen delivery status
    frappe.enqueue(
        update_shopware_delivery_status,
        order_id=shopware_order_id,
        action="reopen",
        queue="short",
        timeout=60
    )


def on_sales_invoice_submit(doc, method=None):
    """
    Hook: When Sales Invoice is submitted (and paid), update Shopware transaction status.
    """
    if not is_shopware_enabled():
        return

    # Check if this is a paid invoice linked to a Shopware order
    # Only update if invoice is paid
    if doc.outstanding_amount > 0:
        return  # Not fully paid yet

    # Get Shopware order ID from linked Sales Order
    sales_order_name = None
    for item in doc.items:
        if item.sales_order:
            sales_order_name = item.sales_order
            break

    if not sales_order_name:
        return

    shopware_order_id = frappe.db.get_value("Sales Order", sales_order_name, ORDER_ID_FIELD)
    if not shopware_order_id:
        return

    # Update Shopware transaction status to paid
    frappe.enqueue(
        update_shopware_transaction_status,
        order_id=shopware_order_id,
        action="pay",
        queue="short",
        timeout=60
    )

    create_shopware_log(
        status="Queued",
        method="on_sales_invoice_submit",
        message=f"Payment status update queued for Shopware order {shopware_order_id}"
    )


def on_sales_order_cancel(doc, method=None):
    """
    Hook: When Sales Order is cancelled, update Shopware order status.
    """
    if not is_shopware_enabled():
        return

    shopware_order_id = doc.get(ORDER_ID_FIELD)
    if not shopware_order_id:
        return

    # Update Shopware order status to cancelled
    frappe.enqueue(
        update_shopware_order_status,
        order_id=shopware_order_id,
        action="cancel",
        queue="short",
        timeout=60
    )

    create_shopware_log(
        status="Queued",
        method="on_sales_order_cancel",
        message=f"Order cancellation queued for Shopware order {shopware_order_id}"
    )


def on_payment_entry_submit(doc, method=None):
    """
    Hook: When Payment Entry is submitted, update Shopware transaction status.
    """
    if not is_shopware_enabled():
        return

    # Check if this payment is for a Shopware order
    for ref in doc.references:
        if ref.reference_doctype == "Sales Invoice":
            invoice = frappe.get_doc("Sales Invoice", ref.reference_name)

            # Get linked Sales Order
            sales_order_name = None
            for item in invoice.items:
                if item.sales_order:
                    sales_order_name = item.sales_order
                    break

            if not sales_order_name:
                continue

            shopware_order_id = frappe.db.get_value("Sales Order", sales_order_name, ORDER_ID_FIELD)
            if not shopware_order_id:
                continue

            # Check if invoice is now fully paid
            invoice.reload()
            if invoice.outstanding_amount <= 0:
                frappe.enqueue(
                    update_shopware_transaction_status,
                    order_id=shopware_order_id,
                    action="pay",
                    queue="short",
                    timeout=60
                )

                create_shopware_log(
                    status="Queued",
                    method="on_payment_entry_submit",
                    message=f"Payment status update queued for Shopware order {shopware_order_id}"
                )
