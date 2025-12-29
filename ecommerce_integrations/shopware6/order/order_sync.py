"""
Shopware 6 Order Sync

Main ShopwareOrder class and order synchronization functions.
Follows the Shopify integration pattern.
"""

from typing import Any, Dict, Optional

import frappe
from frappe import _
from frappe.utils import getdate, nowdate

from lib_shopware6_api_base import Shopware6AdminAPIClientBase, Criteria

from ecommerce_integrations.shopware6.connection import temp_shopware_session, get_shopware_client
from ecommerce_integrations.shopware6.constants import SETTING_DOCTYPE, SERVICE_PRODUCTS
from ecommerce_integrations.shopware6.utils import (
    create_shopware_log,
    update_shopware_log,
    format_shopware_datetime,
)
from ecommerce_integrations.shopware6.customer import (
    get_customer_from_shopware_order,
    create_or_update_billing_contact,
    ShopwareCustomer,
)
from ecommerce_integrations.shopware6.product import create_items_if_not_exist

from ecommerce_integrations.shopware6.order.order_mapper import (
    get_checkout_custom_field,
    get_payment_method_info,
    calculate_delivery_date,
    extract_order_currency,
)
from ecommerce_integrations.shopware6.order.line_item_handler import add_order_item
from ecommerce_integrations.shopware6.order.tax_handler import add_order_taxes
from ecommerce_integrations.shopware6.order.service_items import (
    add_service_items_if_needed,
    detect_service_items_from_line_items,
)
from ecommerce_integrations.shopware6.order.delivery_handler import create_delivery_note
from ecommerce_integrations.shopware6.order.payment_handler import (
    create_sales_invoice,
    verify_payment_status_from_shopware,
)


class ShopwareOrder:
    """
    Shopware order handler following Shopify integration patterns.

    Encapsulates all order sync logic into a reusable class.

    Usage:
        order = ShopwareOrder(order_id="abc123")
        if not order.is_synced():
            order.sync()
    """

    def __init__(self, order_id: str, sales_order_name: str = None):
        """
        Initialize ShopwareOrder.

        Args:
            order_id: Shopware order ID
            sales_order_name: Existing ERPNext Sales Order name (optional)
        """
        self.order_id = order_id
        self.sales_order_name = sales_order_name or self._get_existing_sales_order()
        self.setting = frappe.get_cached_doc(SETTING_DOCTYPE)

        if not self.setting.is_enabled():
            frappe.throw(_("Shopware integration is not enabled."))

    def _get_existing_sales_order(self) -> Optional[str]:
        """Check if order is already synced."""
        return frappe.db.get_value("Sales Order", {"shopware_order_id": self.order_id}, "name")

    def is_synced(self) -> bool:
        """Check if order is already synced to ERPNext."""
        return bool(self.sales_order_name)

    @temp_shopware_session
    def sync(self, client) -> Optional[str]:
        """
        Sync order from Shopware to ERPNext.

        Args:
            client: Shopware API client

        Returns:
            Sales Order name if successful, None otherwise
        """
        if self.is_synced():
            return self.sales_order_name

        return sync_order_by_id(client, self.order_id)


@temp_shopware_session
def sync_order_by_id(client: Shopware6AdminAPIClientBase, order_id: str) -> Optional[str]:
    """
    Sync a specific order from Shopware by ID.

    Args:
        client: Shopware API client
        order_id: Shopware order ID

    Returns:
        ERPNext Sales Order name if created
    """
    # Check if already synced
    existing = frappe.db.get_value("Sales Order", {"shopware_order_id": order_id}, "name")
    if existing:
        return existing

    # Fetch order with associations
    criteria = _build_order_fetch_criteria(order_id)
    response = client.request_post("search/order", criteria)
    orders = response.get("data", [])

    if not orders:
        frappe.throw(_("Order not found in Shopware: {0}").format(order_id))

    order_data = orders[0]
    return create_sales_order(order_data)


def _build_order_fetch_criteria(order_id: str) -> Criteria:
    """Build criteria for fetching a single order with all associations."""
    criteria = Criteria(ids=[order_id])
    criteria.associations["lineItems"] = Criteria()
    criteria.associations["orderCustomer"] = Criteria()
    criteria.associations["orderCustomer"].associations["salutation"] = Criteria()
    criteria.associations["orderCustomer"].associations["customer"] = Criteria()
    criteria.associations["billingAddress"] = Criteria()
    criteria.associations["billingAddress"].associations["country"] = Criteria()
    criteria.associations["billingAddress"].associations["countryState"] = Criteria()
    criteria.associations["billingAddress"].associations["salutation"] = Criteria()
    criteria.associations["deliveries"] = Criteria()
    criteria.associations["deliveries"].associations["shippingOrderAddress"] = Criteria()
    criteria.associations["deliveries"].associations["shippingOrderAddress"].associations["country"] = Criteria()
    criteria.associations["deliveries"].associations["shippingOrderAddress"].associations["countryState"] = Criteria()
    criteria.associations["deliveries"].associations["shippingOrderAddress"].associations["salutation"] = Criteria()
    criteria.associations["deliveries"].associations["shippingMethod"] = Criteria()
    criteria.associations["transactions"] = Criteria()
    criteria.associations["transactions"].associations["paymentMethod"] = Criteria()
    criteria.associations["currency"] = Criteria()
    # Multi-Storefront: Fetch sales channel info
    criteria.associations["salesChannel"] = Criteria()
    return criteria


def create_sales_order(order_data: Dict[str, Any]) -> str:
    """
    Create ERPNext Sales Order from Shopware order data.

    Args:
        order_data: Shopware order object

    Returns:
        Sales Order name
    """
    setting = frappe.get_doc(SETTING_DOCTYPE)

    order_id = order_data.get("id")
    order_number = order_data.get("orderNumber", "")

    # Check if already synced
    existing = frappe.db.get_value("Sales Order", {"shopware_order_id": order_id}, "name")
    if existing:
        frappe.logger("shopware6").info(f"Order {order_id} already exists as {existing}, skipping creation")
        return existing

    # Sync items first
    create_items_if_not_exist(order_data)

    # Get or create customer
    customer = get_customer_from_shopware_order(order_data)

    # Get order date
    order_date = format_shopware_datetime(order_data.get("orderDateTime")) or nowdate()

    # Get currency
    currency_code = extract_order_currency(order_data)

    # Create Sales Order
    so = frappe.new_doc("Sales Order")
    so.naming_series = setting.sales_order_series
    so.customer = customer
    so.company = setting.company
    so.transaction_date = getdate(order_date)
    # Calculate delivery date based on longest item lead time
    so.delivery_date = getdate(
        calculate_delivery_date(order_date, order_data.get("lineItems", []))
    )
    so.currency = currency_code
    so.shopware_order_id = order_id
    so.shopware_order_number = order_number

    # Multi-Storefront: Extract and set Sales Channel info
    sales_channel_id = order_data.get("salesChannelId", "")
    sales_channel_name = ""
    if sales_channel_id:
        # Try to get name from associations first
        sales_channel_data = order_data.get("salesChannel", {})
        if sales_channel_data:
            sales_channel_name = (
                sales_channel_data.get("name") or
                sales_channel_data.get("translated", {}).get("name", "")
            )
        # If not in associations, look up from settings
        if not sales_channel_name:
            for sc in setting.sales_channels or []:
                if sc.sales_channel_id == sales_channel_id:
                    sales_channel_name = sc.sales_channel_name
                    break
    so.shopware_sales_channel_id = sales_channel_id
    so.shopware_sales_channel_name = sales_channel_name

    # Extract and set payment method info
    payment_method_name, erpnext_mode, payment_status = get_payment_method_info(order_data)
    so.shopware_payment_method = payment_method_name
    so.shopware_payment_status = payment_status

    # Set the standard ERPNext mode_of_payment field
    so.mode_of_payment = erpnext_mode

    # Also store in custom field for Payment Entry creation
    if hasattr(so, 'shopware_erpnext_mode_of_payment'):
        so.shopware_erpnext_mode_of_payment = erpnext_mode

    # Extract custom fields from Shopware order
    customer_po_no = get_checkout_custom_field(order_data, "po_number")
    tel_avis_requested = bool(get_checkout_custom_field(order_data, "tel_avis"))
    forklift_requested = bool(get_checkout_custom_field(order_data, "forklift"))
    invoice_email = get_checkout_custom_field(order_data, "invoice_email")

    # Also check if service products exist in line items
    line_items = order_data.get("lineItems", [])
    detected_tel_avis, detected_forklift = detect_service_items_from_line_items(line_items, setting)
    tel_avis_requested = tel_avis_requested or detected_tel_avis
    forklift_requested = forklift_requested or detected_forklift

    # Set custom fields on Sales Order
    if customer_po_no:
        so.customer_po_no = customer_po_no
        # Also set po_no if it exists (standard ERPNext field)
        so_meta = frappe.get_meta("Sales Order")
        if so_meta.has_field("po_no"):
            so.po_no = customer_po_no

    so.tel_avis_requested = 1 if tel_avis_requested else 0
    so.forklift_requested = 1 if forklift_requested else 0

    # Handle invoice_email: Create or update a Billing Contact
    if invoice_email and customer:
        _handle_invoice_email(customer, invoice_email, order_data)

    # Add line items
    for line_item in order_data.get("lineItems", []):
        add_order_item(so, line_item, setting)

    # Add service items if requested (Tel. Avis, Forklift/Hebebühne)
    add_service_items_if_needed(so, setting, order_data, tel_avis_requested, forklift_requested)

    # Add taxes
    add_order_taxes(so, order_data, setting)

    # Set cost center
    if setting.cost_center:
        for item in so.items:
            item.cost_center = setting.cost_center

    try:
        so.insert(ignore_permissions=True)
        so.submit()

        # Create Delivery Note if configured and order is shipped
        if setting.sync_delivery_note:
            _create_delivery_note_if_shipped(so.name, order_data, setting)

        # Create Sales Invoice if configured and order is paid
        if setting.sync_sales_invoice:
            _create_invoice_if_paid(so.name, order_data, setting)

        frappe.db.commit()

        create_shopware_log(
            status="Success",
            method="create_sales_order",
            message=f"Created Sales Order {so.name} from Shopware order {order_number}",
            request_data={"order_id": order_id, "order_number": order_number},
        )

        # Schedule async verification of payment status
        # This handles the race condition where payment webhook arrived before order was created
        frappe.enqueue(
            "ecommerce_integrations.shopware6.order.payment_handler.verify_payment_status_from_shopware",
            order_id=order_id,
            sales_order_name=so.name,
            queue="short",
            timeout=60,
            enqueue_after_commit=True,
        )

        return so.name

    except Exception as e:
        frappe.db.rollback()
        create_shopware_log(
            status="Error",
            method="create_sales_order",
            exception=str(e),
            request_data=order_data,
            rollback=True,
        )
        raise


def _handle_invoice_email(customer: str, invoice_email: str, order_data: Dict[str, Any]) -> None:
    """Create or update billing contact with invoice email."""
    try:
        order_customer = order_data.get("orderCustomer", {})
        create_or_update_billing_contact(
            customer=customer,
            billing_email=invoice_email,
            customer_data=order_customer
        )

        # Also store on customer for backward compatibility
        if frappe.db.has_column("Customer", "invoice_email"):
            frappe.db.set_value("Customer", customer, "invoice_email", invoice_email)
    except Exception as e:
        frappe.log_error(
            f"Failed to create/update billing contact for {customer}: {e}",
            "Shopware Order Sync"
        )


def _create_delivery_note_if_shipped(
    sales_order: str,
    order_data: Dict[str, Any],
    setting
) -> None:
    """Create Delivery Note if order is shipped."""
    deliveries = order_data.get("deliveries") or []
    for delivery in deliveries:
        state = delivery.get("stateMachineState") or {}
        if state.get("technicalName") in ["shipped", "delivered"]:
            create_delivery_note(sales_order, delivery, setting)
            break


def _create_invoice_if_paid(
    sales_order: str,
    order_data: Dict[str, Any],
    setting
) -> None:
    """Create Sales Invoice if order is paid."""
    transactions = order_data.get("transactions") or []
    for transaction in transactions:
        state = transaction.get("stateMachineState") or {}
        if state.get("technicalName") in ["paid", "completed"]:
            create_sales_invoice(sales_order, transaction, setting)
            break


def sync_order_from_webhook(payload: Dict[str, Any], request_id: str = None):
    """
    Handle order sync from Shopware webhook.

    Handles both new orders (order.placed) and updates (order.updated).
    For updates, it will update the custom fields on existing Sales Orders.

    Args:
        payload: Webhook payload from Shopware
        request_id: Log entry ID for tracking
    """
    frappe.set_user("Administrator")
    frappe.flags.request_id = request_id

    try:
        # Support multiple payload formats
        order_id = _extract_order_id_from_payload(payload)

        # Check if this is an update (custom fields update after order creation)
        is_update = payload.get("data", {}).get("isUpdate", False)

        # Get custom fields from webhook payload (for update events)
        webhook_custom_fields = payload.get("data", {}).get("customFields", {})

        if order_id:
            # Check if order already exists in ERPNext
            existing_so = frappe.db.get_value("Sales Order", {"shopware_order_id": order_id}, "name")

            if existing_so and is_update:
                # This is an update - update the custom fields on existing Sales Order
                update_order_custom_fields(existing_so, order_id, webhook_custom_fields, request_id)
            elif existing_so:
                # Order already synced, skip
                if request_id:
                    update_shopware_log(request_id, status="Skipped", message=f"Order {order_id} already synced as {existing_so}")
            else:
                # New order - sync it
                sync_order_by_id(order_id)
                if request_id:
                    update_shopware_log(request_id, status="Success", message=f"Synced order {order_id}")
        else:
            if request_id:
                update_shopware_log(request_id, status="Error", message="No order ID in webhook payload")

    except Exception as e:
        if request_id:
            update_shopware_log(request_id, status="Error", exception=str(e))
        raise


def _extract_order_id_from_payload(payload: Dict[str, Any]) -> Optional[str]:
    """Extract order ID from various webhook payload formats."""
    # Direct: primaryKey or id
    order_id = payload.get("primaryKey") or payload.get("id")
    if order_id:
        return order_id

    # Shopware native: payload.entity.id
    entity = payload.get("payload", {}).get("entity", {})
    order_id = entity.get("id")
    if order_id:
        return order_id

    # Custom webhook format from ERPNextWebhookSubscriber
    data = payload.get("data", {})
    return data.get("orderId")


def update_order_custom_fields(
    sales_order_name: str,
    shopware_order_id: str,
    webhook_custom_fields: Dict[str, Any],
    request_id: str = None
) -> None:
    """
    Update custom fields on an existing Sales Order.

    This is called when custom fields are saved AFTER the initial order sync.
    It updates the tel_avis_requested, forklift_requested, customer_po_no fields
    and creates/updates the billing contact if invoice_email is set.

    Args:
        sales_order_name: ERPNext Sales Order name
        shopware_order_id: Shopware order ID
        webhook_custom_fields: Custom fields from webhook payload
        request_id: Log entry ID for tracking
    """
    try:
        so = frappe.get_doc("Sales Order", sales_order_name)
        setting = frappe.get_doc(SETTING_DOCTYPE)

        # Extract custom fields
        customer_po_no = webhook_custom_fields.get("custom_po_number")
        tel_avis_requested = bool(webhook_custom_fields.get("custom_tel_avis"))
        forklift_requested = bool(webhook_custom_fields.get("custom_forklift_required"))
        invoice_email = webhook_custom_fields.get("invoice_email")

        updates_made = []

        # Update PO number if set and different
        if customer_po_no and so.get("customer_po_no") != customer_po_no:
            frappe.db.set_value("Sales Order", sales_order_name, "customer_po_no", customer_po_no)
            if frappe.get_meta("Sales Order").has_field("po_no"):
                frappe.db.set_value("Sales Order", sales_order_name, "po_no", customer_po_no)
            updates_made.append(f"customer_po_no={customer_po_no}")

        # Update Tel. Avis flag
        if tel_avis_requested and not so.get("tel_avis_requested"):
            frappe.db.set_value("Sales Order", sales_order_name, "tel_avis_requested", 1)
            updates_made.append("tel_avis_requested=1")

        # Update Forklift flag
        if forklift_requested and not so.get("forklift_requested"):
            frappe.db.set_value("Sales Order", sales_order_name, "forklift_requested", 1)
            updates_made.append("forklift_requested=1")

        # Handle billing contact if invoice_email is set
        if invoice_email and so.customer:
            shopware_customer_id = frappe.db.get_value("Customer", so.customer, "shopware_customer_id")
            if shopware_customer_id:
                customer_obj = ShopwareCustomer(customer_id=shopware_customer_id)
                customer_obj.create_or_update_billing_contact(billing_email=invoice_email)
            else:
                create_or_update_billing_contact(
                    customer=so.customer,
                    billing_email=invoice_email,
                )
            if frappe.db.has_column("Customer", "invoice_email"):
                frappe.db.set_value("Customer", so.customer, "invoice_email", invoice_email)
            updates_made.append(f"invoice_email={invoice_email}")

        if updates_made:
            frappe.db.commit()
            message = f"Updated {sales_order_name}: {', '.join(updates_made)}"
            frappe.logger("shopware6").info(message)
            if request_id:
                update_shopware_log(request_id, status="Success", message=message)
        else:
            if request_id:
                update_shopware_log(request_id, status="Skipped", message=f"No custom field updates for {sales_order_name}")

    except Exception as e:
        frappe.log_error(f"Failed to update custom fields for {sales_order_name}: {e}", "Shopware Order Update")
        if request_id:
            update_shopware_log(request_id, status="Error", exception=str(e))


def update_order_status(payload: Dict[str, Any], request_id: str = None):
    """
    Handle order state change from Shopware webhook.

    Args:
        payload: Webhook payload from Shopware
        request_id: Log entry ID for tracking
    """
    from ecommerce_integrations.shopware6.constants import PAYMENT_STATE_MAP

    frappe.set_user("Administrator")
    frappe.flags.request_id = request_id

    try:
        order_id = _extract_order_id_from_payload(payload)

        if order_id:
            # Find linked Sales Order
            sales_order = frappe.db.get_value(
                "Sales Order",
                {"shopware_order_id": order_id},
                "name"
            )

            if sales_order:
                # Get new payment state from payload
                new_state = ""
                payment_state = ""

                # Try different payload formats
                entity = payload.get("payload", {}).get("entity", {})
                state_machine_state = entity.get("stateMachineState", {})
                new_state = state_machine_state.get("technicalName", "")

                # For payment state change webhook
                data = payload.get("data", {})
                payment_state = data.get("paymentState", "") or new_state

                # Map to ERPNext payment status
                if payment_state:
                    erpnext_status = PAYMENT_STATE_MAP.get(payment_state, None)
                    if erpnext_status:
                        frappe.db.set_value(
                            "Sales Order",
                            sales_order,
                            "shopware_payment_status",
                            erpnext_status
                        )

                        # If payment is now "Paid" and auto-invoice is enabled, create invoice
                        if erpnext_status == "Paid":
                            setting = frappe.get_doc(SETTING_DOCTYPE)
                            if setting.sync_sales_invoice:
                                try:
                                    create_sales_invoice(sales_order, {"id": order_id}, setting)
                                except Exception as e:
                                    frappe.log_error(
                                        f"Failed to create invoice for {sales_order} after payment: {e}",
                                        "Shopware Payment Status Update"
                                    )

                if request_id:
                    update_shopware_log(
                        request_id,
                        status="Success",
                        message=f"Order state update processed for {sales_order}, payment status: {payment_state or new_state}"
                    )
            else:
                if request_id:
                    update_shopware_log(request_id, status="Skipped", message=f"No Sales Order found for Shopware order {order_id}")

    except Exception as e:
        if request_id:
            update_shopware_log(request_id, status="Error", exception=str(e))
        raise
