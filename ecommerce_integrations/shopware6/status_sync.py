"""
Status Sync: ERPNext -> Shopware

Syncs document status changes from ERPNext to Shopware:
- Delivery Note submit -> Shopware delivery status "shipped" + auto-create Sales Invoice
- Sales Invoice submit -> Shopware transaction status (optionally "paid" if fully paid)
- Sales Order cancel -> Shopware order status "cancelled"

Invoice Creation Policy (German Law Compliant):
- Invoices are created after shipment (Delivery Note), not after payment
- The delivery date (Lieferdatum) is the date goods are handed to carrier
- This ensures correct VAT timing per § 31 Abs. 4 UStDV
"""

import frappe

from ecommerce_integrations.shopware6.connection import temp_shopware_session
from ecommerce_integrations.shopware6.constants import (
    INVOICE_CREATE_ENQUEUE_TIMEOUT,
    ORDER_ID_FIELD,
    SETTING_DOCTYPE,
    STATUS_SYNC_ENQUEUE_TIMEOUT,
)
from ecommerce_integrations.shopware6.utils import create_shopware_log, get_logger


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
    logger = get_logger("update_shopware_order_status")
    try:
        # Shopware uses state machine transitions
        # POST /_action/state-machine/order/{orderId}/state/{action}
        endpoint = f"_action/state-machine/order/{order_id}/state/{action}"
        client.request_post(endpoint, {})
        logger.info(f"Updated Shopware order {order_id} status to {action}")
        return True
    except Exception as e:
        logger.error(
            f"Failed to update Shopware order {order_id} status to {action}",
            exception=e,
            persist=True
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
    logger = get_logger("update_shopware_delivery_status")
    try:
        # First get the delivery ID for this order
        response = client.request_post("search/order-delivery", {
            "filter": [{"type": "equals", "field": "orderId", "value": order_id}],
            "limit": 1
        })

        deliveries = response.get("data", [])
        if not deliveries:
            logger.warning(
                f"No delivery found for Shopware order {order_id}",
                persist=True
            )
            return False

        delivery_id = deliveries[0].get("id")

        # Update delivery state
        endpoint = f"_action/state-machine/order_delivery/{delivery_id}/state/{action}"
        client.request_post(endpoint, {})
        logger.info(f"Updated Shopware delivery for order {order_id} to {action}")
        return True
    except Exception as e:
        logger.error(
            f"Failed to update Shopware delivery for order {order_id} to {action}",
            exception=e,
            persist=True
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
    logger = get_logger("update_shopware_transaction_status")
    try:
        # First get the transaction ID for this order
        response = client.request_post("search/order-transaction", {
            "filter": [{"type": "equals", "field": "orderId", "value": order_id}],
            "limit": 1
        })

        transactions = response.get("data", [])
        if not transactions:
            logger.warning(
                f"No transaction found for Shopware order {order_id}",
                persist=True
            )
            return False

        transaction_id = transactions[0].get("id")

        # Update transaction state
        endpoint = f"_action/state-machine/order_transaction/{transaction_id}/state/{action}"
        client.request_post(endpoint, {})
        logger.info(f"Updated Shopware transaction for order {order_id} to {action}")
        return True
    except Exception as e:
        logger.error(
            f"Failed to update Shopware transaction for order {order_id} to {action}",
            exception=e,
            persist=True
        )
        return False


def on_delivery_note_submit(doc, method=None):
    """
    Hook: When Delivery Note is submitted:
    1. Update Shopware delivery status to 'shipped'
    2. Auto-create Sales Invoice (German law: invoice after shipment, not payment)
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

    # 1. Update Shopware delivery status
    frappe.enqueue(
        update_shopware_delivery_status,
        order_id=shopware_order_id,
        action="ship",
        queue="short",
        timeout=STATUS_SYNC_ENQUEUE_TIMEOUT,
    )

    create_shopware_log(
        status="Queued",
        method="on_delivery_note_submit",
        message=f"Delivery status update queued for Shopware order {shopware_order_id}"
    )

    # 2. Auto-create Sales Invoice from Delivery Note
    # This is the legally correct approach: invoice after shipment (Versand)
    setting = frappe.get_cached_doc(SETTING_DOCTYPE)
    if getattr(setting, "auto_create_invoice_on_shipment", True):
        frappe.enqueue(
            create_invoice_from_delivery_note,
            delivery_note_name=doc.name,
            sales_order_name=sales_order_name,
            shopware_order_id=shopware_order_id,
            queue="short",
            timeout=INVOICE_CREATE_ENQUEUE_TIMEOUT,
        )


def create_invoice_from_delivery_note(
    delivery_note_name: str,
    sales_order_name: str,
    shopware_order_id: str
) -> str | None:
    """
    Create Sales Invoice from Delivery Note after shipment.

    This is the legally correct approach for German E-Commerce:
    - Invoice is created after goods are shipped (Lieferdatum = Versanddatum)
    - VAT liability arises at time of delivery to carrier
    - Invoice must show correct delivery date per § 31 Abs. 4 UStDV

    Args:
        delivery_note_name: ERPNext Delivery Note name
        sales_order_name: ERPNext Sales Order name
        shopware_order_id: Shopware order ID

    Returns:
        Sales Invoice name if created, None if already exists or error
    """
    logger = get_logger("create_invoice_from_delivery_note")

    try:
        # Check if Sales Invoice already exists for this Delivery Note
        existing_si = frappe.db.get_value(
            "Sales Invoice Item",
            {"delivery_note": delivery_note_name, "docstatus": ["!=", 2]},
            "parent"
        )
        if existing_si:
            logger.info(f"Sales Invoice {existing_si} already exists for DN {delivery_note_name}")
            return existing_si

        # Also check by Sales Order (fallback)
        existing_si_by_so = frappe.db.get_value(
            "Sales Invoice Item",
            {"sales_order": sales_order_name, "docstatus": ["!=", 2]},
            "parent"
        )
        if existing_si_by_so:
            logger.info(f"Sales Invoice {existing_si_by_so} already exists for SO {sales_order_name}")
            return existing_si_by_so

        # Create Sales Invoice from Delivery Note
        from erpnext.stock.doctype.delivery_note.delivery_note import make_sales_invoice

        si = make_sales_invoice(delivery_note_name)

        # Apply settings
        setting = frappe.get_cached_doc(SETTING_DOCTYPE)
        if hasattr(setting, "sales_invoice_series") and setting.sales_invoice_series:
            si.naming_series = setting.sales_invoice_series

        # Copy mode_of_payment from Sales Order
        so_mode_of_payment = frappe.db.get_value("Sales Order", sales_order_name, "mode_of_payment")
        if so_mode_of_payment:
            si.mode_of_payment = so_mode_of_payment

        # Link to Shopware order for tracking
        if hasattr(si, "shopware_order_id"):
            si.shopware_order_id = shopware_order_id

        si.insert(ignore_permissions=True)
        si.submit()

        logger.info(f"Created Sales Invoice {si.name} from Delivery Note {delivery_note_name}")

        create_shopware_log(
            status="Success",
            method="create_invoice_from_delivery_note",
            message=f"Created Sales Invoice {si.name} from DN {delivery_note_name} (Shopware order: {shopware_order_id})"
        )

        return si.name

    except Exception as e:
        logger.error(
            f"Failed to create Sales Invoice from Delivery Note {delivery_note_name}",
            exception=e,
            persist=True
        )
        create_shopware_log(
            status="Error",
            method="create_invoice_from_delivery_note",
            message=f"Failed to create invoice from DN {delivery_note_name}: {e!s}"
        )
        return None


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
        timeout=STATUS_SYNC_ENQUEUE_TIMEOUT,
    )


def on_sales_invoice_submit(doc, method=None):
    """
    Hook: When Sales Invoice is submitted, update Shopware order status.

    New behavior (German law compliant):
    - Invoice created = Order is "in_progress" (being processed)
    - Invoice is only marked "paid" when Payment Entry is created

    The old behavior (waiting for outstanding_amount == 0) is removed because:
    - Invoices should be created after shipment, not after payment
    - Payment status is tracked separately via on_payment_entry_submit
    """
    if not is_shopware_enabled():
        return

    # Get Shopware order ID from linked Sales Order or Delivery Note
    sales_order_name = None
    for item in doc.items:
        if item.sales_order:
            sales_order_name = item.sales_order
            break
        # Also check delivery_note link (if invoice was created from DN)
        if item.delivery_note:
            dn_so = frappe.db.get_value(
                "Delivery Note Item",
                {"parent": item.delivery_note},
                "against_sales_order"
            )
            if dn_so:
                sales_order_name = dn_so
                break

    if not sales_order_name:
        return

    shopware_order_id = frappe.db.get_value("Sales Order", sales_order_name, ORDER_ID_FIELD)
    if not shopware_order_id:
        return

    # Update Shopware order status to "in_progress" (invoice created, processing order)
    frappe.enqueue(
        update_shopware_order_status,
        order_id=shopware_order_id,
        action="process",
        queue="short",
        timeout=STATUS_SYNC_ENQUEUE_TIMEOUT,
    )

    create_shopware_log(
        status="Queued",
        method="on_sales_invoice_submit",
        message=f"Order status update (invoiced) queued for Shopware order {shopware_order_id}"
    )

    # If invoice is already fully paid (e.g., prepaid orders), also update payment status
    if doc.outstanding_amount <= 0 and doc.grand_total > 0:
        frappe.enqueue(
            update_shopware_transaction_status,
            order_id=shopware_order_id,
            action="pay",
            queue="short",
            timeout=STATUS_SYNC_ENQUEUE_TIMEOUT,
        )

        create_shopware_log(
            status="Queued",
            method="on_sales_invoice_submit",
            message=f"Payment status update queued for prepaid Shopware order {shopware_order_id}"
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
        timeout=STATUS_SYNC_ENQUEUE_TIMEOUT,
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
                    timeout=STATUS_SYNC_ENQUEUE_TIMEOUT,
                )

                create_shopware_log(
                    status="Queued",
                    method="on_payment_entry_submit",
                    message=f"Payment status update queued for Shopware order {shopware_order_id}"
                )


def on_sales_order_submit(doc, method=None):
    """
    Hook: When Sales Order is submitted (e.g., via workflow approval),
    create Delivery Note and/or Sales Invoice if configured.

    This handles the case where orders from Shopware are created in
    "Pending Approval" state and later approved via workflow.
    """
    if not is_shopware_enabled():
        return

    shopware_order_id = doc.get(ORDER_ID_FIELD)
    if not shopware_order_id:
        return  # Not a Shopware order

    try:
        setting = frappe.get_cached_doc(SETTING_DOCTYPE)

        # Fetch current order data from Shopware
        from shopware6_api_client import Criteria

        from ecommerce_integrations.shopware6.connection import temp_shopware_session
        from ecommerce_integrations.shopware6.order.order_sync import (
            _create_delivery_note_if_shipped,
            _create_invoice_if_paid,
        )

        @temp_shopware_session
        def fetch_order_data(client, order_id):
            """Fetch order data from Shopware to check delivery/payment status."""
            criteria = Criteria()
            criteria.ids = [order_id]
            criteria.associations["deliveries"] = Criteria()
            criteria.associations["deliveries"].associations["stateMachineState"] = Criteria()
            criteria.associations["transactions"] = Criteria()
            criteria.associations["transactions"].associations["stateMachineState"] = Criteria()
            criteria.associations["transactions"].associations["paymentMethod"] = Criteria()

            result = client.order.search(criteria=criteria)
            orders = result.get("data", [])
            return orders[0] if orders else None

        order_data = fetch_order_data(order_id=shopware_order_id)

        if not order_data:
            get_logger("on_sales_order_submit").warning(
                f"Could not fetch Shopware order data for {shopware_order_id}"
            )
            return

        # Create Delivery Note if configured and order is shipped
        if setting.sync_delivery_note:
            _create_delivery_note_if_shipped(doc.name, order_data, setting)

        # Create Sales Invoice if configured and order is paid
        if setting.sync_sales_invoice:
            _create_invoice_if_paid(doc.name, order_data, setting)

        create_shopware_log(
            status="Success",
            method="on_sales_order_submit",
            message=f"Processed post-approval sync for Sales Order {doc.name}"
        )

    except Exception as e:
        get_logger("on_sales_order_submit").error(
            f"Error processing post-approval sync for {doc.name}: {e}",
            persist=True
        )
