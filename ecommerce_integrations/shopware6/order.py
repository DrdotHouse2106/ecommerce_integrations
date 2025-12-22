"""
Shopware 6 Order Sync Module

Handles synchronization of orders between Shopware 6 and ERPNext.
Creates Sales Orders, optionally Delivery Notes and Sales Invoices.
"""

from typing import Any, Dict, List, Optional

import frappe
from frappe import _
from frappe.utils import flt, nowdate, getdate

from lib_shopware6_api_base import (
    Shopware6AdminAPIClientBase,
    Criteria,
    EqualsFilter,
    RangeFilter,
)

from ecommerce_integrations.shopware6.connection import temp_shopware_session, get_shopware_client
from ecommerce_integrations.shopware6.constants import (
    MODULE_NAME,
    SETTING_DOCTYPE,
    ORDER_STATE_MAP,
    PAYMENT_STATE_MAP,
    PAYMENT_METHOD_MAP,
    DEFAULT_MODE_OF_PAYMENT,
    SERVICE_PRODUCTS,
    SHOPWARE_CHECKOUT_CUSTOM_FIELDS,
)
from ecommerce_integrations.shopware6.customer import (
    get_customer_from_shopware_order,
    create_or_update_billing_contact,
    ShopwareCustomer,
)
from ecommerce_integrations.shopware6.product import create_items_if_not_exist, get_item_code
from ecommerce_integrations.shopware6.utils import (
    create_shopware_log,
    update_shopware_log,
    format_shopware_datetime,
    get_price_from_shopware_price_object,
    get_net_unit_price_from_line_item,
    get_tax_rate_from_line_item,
    convert_gross_to_net,
)


def get_checkout_custom_field(order_data: dict, field_type: str) -> Any:
    """
    Extract a custom field value from Shopware order data.
    
    Shopware custom fields can be in multiple locations:
    - order.customFields
    - order.orderCustomer.customFields
    - Direct order payload fields (for webhook payloads)
    
    Args:
        order_data: Shopware order object
        field_type: Type of field to extract (po_number, tel_avis, forklift, invoice_email)
    
    Returns:
        Field value or None if not found
    """
    field_names = SHOPWARE_CHECKOUT_CUSTOM_FIELDS.get(field_type, [])
    
    # Check order customFields
    custom_fields = order_data.get("customFields", {}) or {}
    for field_name in field_names:
        if field_name in custom_fields:
            return custom_fields[field_name]
    
    # Check orderCustomer customFields
    order_customer = order_data.get("orderCustomer", {}) or {}
    customer_custom_fields = order_customer.get("customFields", {}) or {}
    for field_name in field_names:
        if field_name in customer_custom_fields:
            return customer_custom_fields[field_name]
    
    # Check direct payload fields (for webhook format)
    for field_name in field_names:
        if field_name in order_data:
            return order_data[field_name]
    
    return None


def get_payment_method_info(order_data: dict) -> tuple:
    """
    Extract payment method and status from Shopware order.
    
    Args:
        order_data: Shopware order object
    
    Returns:
        tuple: (payment_method_name, erpnext_mode_of_payment, payment_status)
    """
    transactions = order_data.get("transactions", []) or []
    
    if not transactions:
        return None, DEFAULT_MODE_OF_PAYMENT, "Unpaid"
    
    # Get the latest/first transaction
    transaction = transactions[0]
    
    # Get payment method
    payment_method = transaction.get("paymentMethod", {}) or {}
    payment_method_name = payment_method.get("shortName", "") or payment_method.get("name", "")
    
    # Map to ERPNext mode of payment
    erpnext_mode = DEFAULT_MODE_OF_PAYMENT
    payment_method_lower = payment_method_name.lower().replace("-", "_").replace(" ", "_")
    
    # Try direct mapping
    if payment_method_lower in PAYMENT_METHOD_MAP:
        erpnext_mode = PAYMENT_METHOD_MAP[payment_method_lower]
    else:
        # Try partial matching for known payment providers
        for key, value in PAYMENT_METHOD_MAP.items():
            if key in payment_method_lower:
                erpnext_mode = value
                break
    
    # Get payment status
    state = transaction.get("stateMachineState", {}) or {}
    state_name = state.get("technicalName", "open")
    payment_status = PAYMENT_STATE_MAP.get(state_name, "Unpaid")
    
    return payment_method_name, erpnext_mode, payment_status


def add_service_items_if_needed(
    so: "frappe.Document",
    setting,
    order_data: dict,
    tel_avis_requested: bool = False,
    forklift_requested: bool = False
) -> None:
    """
    Add service items (Tel. Avis, Forklift/Hebebühne) to the Sales Order if requested.
    
    The services can come from:
    1. A custom field checkbox in the order
    2. An actual line item with the service product number
    
    Args:
        so: Sales Order document
        setting: Shopware Setting document
        order_data: Shopware order data
        tel_avis_requested: Whether tel avis was requested via custom field
        forklift_requested: Whether forklift was requested via custom field
    """
    line_items = order_data.get("lineItems", [])
    
    # Get all product numbers already in the order
    existing_product_numbers = set()
    for line_item in line_items:
        product_number = line_item.get("payload", {}).get("productNumber", "")
        if product_number:
            existing_product_numbers.add(product_number)
    
    # Handle Tel. Avis service
    if tel_avis_requested and getattr(setting, 'enable_tel_avis_service', True):
        tel_avis_item_code = getattr(setting, 'tel_avis_item', None) or SERVICE_PRODUCTS.get("tel_avis", "SERVICE-TEL-AVIS")
        
        if tel_avis_item_code not in existing_product_numbers:
            # Check if the item exists in ERPNext
            if frappe.db.exists("Item", tel_avis_item_code):
                item_rate = frappe.db.get_value("Item", tel_avis_item_code, "standard_rate")
                if item_rate is None:
                    item_rate = getattr(setting, 'tel_avis_price', 7.50) or 7.50
                so.append(
                    "items",
                    {
                        "item_code": tel_avis_item_code,
                        "qty": 1,
                        "rate": flt(item_rate),
                        "warehouse": setting.warehouse,
                        "delivery_date": so.delivery_date,
                        "description": "Service: Telefonisches Avis",
                    },
                )
            else:
                # Item doesn't exist - log a warning so the admin knows to create it
                frappe.log_error(
                    f"Tel. Avis item '{tel_avis_item_code}' not found in ERPNext. Please create this item.",
                    "Shopware Order Sync - Missing Service Item"
                )
    
    # Handle Forklift/Hebebühne service
    if forklift_requested and getattr(setting, 'enable_forklift_service', True):
        forklift_item_code = getattr(setting, 'forklift_item', None) or SERVICE_PRODUCTS.get("forklift", "SERVICE-FORKLIFT")
        
        if forklift_item_code not in existing_product_numbers:
            # Check if the item exists in ERPNext
            if frappe.db.exists("Item", forklift_item_code):
                item_rate = frappe.db.get_value("Item", forklift_item_code, "standard_rate")
                if item_rate is None:
                    item_rate = getattr(setting, 'forklift_price', 0) or 0
                so.append(
                    "items",
                    {
                        "item_code": forklift_item_code,
                        "qty": 1,
                        "rate": flt(item_rate),
                        "warehouse": setting.warehouse,
                        "delivery_date": so.delivery_date,
                        "description": "Service: Hebebühne / Forklift",
                    },
                )
            else:
                # Item doesn't exist - log a warning so the admin knows to create it
                frappe.log_error(
                    f"Forklift item '{forklift_item_code}' not found in ERPNext. Please create this item.",
                    "Shopware Order Sync - Missing Service Item"
                )


# Keep the old function name as an alias for backwards compatibility
def add_service_item_if_needed(
    so: "frappe.Document",
    setting,
    order_data: dict,
    tel_avis_requested: bool = False
) -> None:
    """Backwards compatible wrapper for add_service_items_if_needed."""
    add_service_items_if_needed(so, setting, order_data, tel_avis_requested, False)


def sync_order_from_webhook(payload: Dict[str, Any], request_id: str = None):
    """
    Handle order sync from Shopware webhook.
    
    Handles both new orders (order.placed) and updates (order.updated).
    For updates, it will update the custom fields on existing Sales Orders.

    Args:
        payload: Webhook payload from Shopware
        request_id: Log entry ID for tracking
    """
    # Set request_id in frappe.flags for downstream access (Shopify pattern)
    frappe.set_user("Administrator")
    frappe.flags.request_id = request_id

    try:
        # Support multiple payload formats:
        # 1. Direct: primaryKey or id
        # 2. Shopware native: payload.entity.id
        # 3. Custom webhook: data.orderId
        order_id = payload.get("primaryKey") or payload.get("id")
        if not order_id:
            entity = payload.get("payload", {}).get("entity", {})
            order_id = entity.get("id")
        if not order_id:
            # Custom webhook format from ERPNextWebhookSubscriber
            data = payload.get("data", {})
            order_id = data.get("orderId")
        
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
            # Also update po_no if field exists
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
            # Get customer's shopware_customer_id to use ShopwareCustomer class
            shopware_customer_id = frappe.db.get_value("Customer", so.customer, "shopware_customer_id")
            if shopware_customer_id:
                customer_obj = ShopwareCustomer(customer_id=shopware_customer_id)
                customer_obj.create_or_update_billing_contact(billing_email=invoice_email)
            else:
                # Fallback to direct function call if no shopware ID
                create_or_update_billing_contact(
                    customer=so.customer,
                    billing_email=invoice_email,
                )
            # Also update customer's invoice_email field
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
    # Set request_id in frappe.flags for downstream access (Shopify pattern)
    frappe.set_user("Administrator")
    frappe.flags.request_id = request_id

    try:
        # Support multiple payload formats
        order_id = payload.get("primaryKey") or payload.get("id")
        if not order_id:
            entity = payload.get("payload", {}).get("entity", {})
            order_id = entity.get("id")
        if not order_id:
            # Custom webhook format from ERPNextWebhookSubscriber
            data = payload.get("data", {})
            order_id = data.get("orderId")

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
                        # Update the Sales Order payment status field
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


@temp_shopware_session
def sync_order_by_id(client: Shopware6AdminAPIClientBase, order_id: str) -> str | None:
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

    response = client.request_post("search/order", criteria)
    orders = response.get("data", [])

    if not orders:
        frappe.throw(_("Order not found in Shopware: {0}").format(order_id))

    order_data = orders[0]
    return create_sales_order(order_data)


def create_sales_order(order_data: dict[str, Any]) -> str:
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
    currency = order_data.get("currency", {})
    currency_code = currency.get("isoCode", "EUR") if isinstance(currency, dict) else "EUR"

    # Create Sales Order
    so = frappe.new_doc("Sales Order")
    so.naming_series = setting.sales_order_series
    so.customer = customer
    so.company = setting.company
    so.transaction_date = getdate(order_date)
    so.delivery_date = getdate(order_date)  # Can be updated later
    so.currency = currency_code
    so.shopware_order_id = order_id
    so.shopware_order_number = order_number

    # Extract and set payment method info
    payment_method_name, erpnext_mode, payment_status = get_payment_method_info(order_data)
    so.shopware_payment_method = payment_method_name
    so.shopware_payment_status = payment_status

    # Set the standard ERPNext mode_of_payment field
    # This is used by print templates and other ERPNext features
    so.mode_of_payment = erpnext_mode

    # Also store in custom field for Payment Entry creation (backwards compatibility)
    if hasattr(so, 'shopware_erpnext_mode_of_payment'):
        so.shopware_erpnext_mode_of_payment = erpnext_mode

    # Extract custom fields from Shopware order
    customer_po_no = get_checkout_custom_field(order_data, "po_number")
    tel_avis_requested = bool(get_checkout_custom_field(order_data, "tel_avis"))
    forklift_requested = bool(get_checkout_custom_field(order_data, "forklift"))
    invoice_email = get_checkout_custom_field(order_data, "invoice_email")
    
    # Also check if service products exist in line items (in case custom fields weren't set yet)
    line_items = order_data.get("lineItems", [])
    for line_item in line_items:
        product_id = line_item.get("productId") or line_item.get("referencedId") or ""
        product_number = line_item.get("payload", {}).get("productNumber", "") if line_item.get("payload") else ""
        
        # Check for Tel. Avis service product
        if not tel_avis_requested:
            tel_avis_item = getattr(setting, 'tel_avis_item', None) or SERVICE_PRODUCTS.get("tel_avis", "SERVICE-TEL-AVIS")
            if product_number == tel_avis_item or "tel" in product_number.lower() or "avis" in product_number.lower():
                tel_avis_requested = True
        
        # Check for Forklift/Hebebühne service product
        if not forklift_requested:
            forklift_item = getattr(setting, 'forklift_item', None) or SERVICE_PRODUCTS.get("forklift", "SERVICE-FORKLIFT")
            if product_number == forklift_item or "forklift" in product_number.lower() or "hebebuehne" in product_number.lower() or "hebebühne" in product_number.lower():
                forklift_requested = True

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
    # This is the proper ERPNext way to have a separate billing email
    if invoice_email and customer:
        try:
            # Get customer data for billing contact name/salutation
            order_customer = order_data.get("orderCustomer", {})
            
            # Create or update the billing contact
            # If the email changes on future orders, the existing billing contact will be updated
            create_or_update_billing_contact(
                customer=customer,
                billing_email=invoice_email,
                customer_data=order_customer
            )
            
            # Also store on customer for backward compatibility / quick reference
            if frappe.db.has_column("Customer", "invoice_email"):
                frappe.db.set_value("Customer", customer, "invoice_email", invoice_email)
        except Exception as e:
            frappe.log_error(
                f"Failed to create/update billing contact for {customer}: {e}",
                "Shopware Order Sync"
            )

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
            deliveries = order_data.get("deliveries") or []
            for delivery in deliveries:
                state = delivery.get("stateMachineState") or {}
                if state.get("technicalName") in ["shipped", "delivered"]:
                    create_delivery_note(so.name, delivery, setting)
                    break

        # Create Sales Invoice if configured and order is paid
        if setting.sync_sales_invoice:
            transactions = order_data.get("transactions") or []
            for transaction in transactions:
                state = transaction.get("stateMachineState") or {}
                if state.get("technicalName") in ["paid", "completed"]:
                    create_sales_invoice(so.name, transaction, setting)
                    break

        frappe.db.commit()

        create_shopware_log(
            status="Success",
            method="create_sales_order",
            message=f"Created Sales Order {so.name} from Shopware order {order_number}",
            request_data={"order_id": order_id, "order_number": order_number},
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


def add_order_item(so: "frappe.Document", line_item: dict[str, Any], setting) -> None:
    """
    Add a line item to the Sales Order.

    Supports both B2B (net prices) and B2C (gross prices) modes.
    When import_prices_as_net is enabled (B2B), Shopware's gross unitPrice
    is converted to net price using the tax rate from the line item.

    Args:
        so: Sales Order document
        line_item: Shopware line item data
        setting: Shopware Setting document
    """
    # Handle different line item types
    item_type = line_item.get("type", "")

    # Handle promotions/discounts (negative line items)
    if item_type in ["promotion", "discount"]:
        _add_discount_item(so, line_item, setting)
        return

    # Skip non-product items (except shipping and discounts handled above)
    if item_type not in ["product", ""]:
        # Handle shipping as item if configured
        if item_type == "shipping" and setting.add_shipping_as_item:
            _add_shipping_item(so, line_item, setting)
        return

    # Get ERPNext item code
    item_code = get_item_code(line_item)
    if not item_code:
        # Try to sync the product
        product_id = line_item.get("productId") or line_item.get("referencedId")
        if product_id:
            from ecommerce_integrations.shopware6.product import ShopwareProduct

            product = ShopwareProduct(product_id)
            product.sync_product()
            item_code = get_item_code(line_item)

    if not item_code:
        frappe.log_error(
            f"Could not find/create item for Shopware product: {line_item.get('productId')}",
            "Shopware Order Sync",
        )
        return

    # Get price - handle B2B (net) vs B2C (gross) mode
    # Shopware's unitPrice is always gross (including tax)
    use_net_prices = getattr(setting, 'import_prices_as_net', True)
    default_tax_rate = flt(getattr(setting, 'default_tax_rate', 19.0))

    if use_net_prices:
        # B2B mode: Convert gross to net price
        unit_price = flt(get_net_unit_price_from_line_item(line_item, default_tax_rate))
    else:
        # B2C mode: Use gross price as-is
        unit_price = flt(line_item.get("unitPrice", 0))

    quantity = flt(line_item.get("quantity", 1))

    so.append(
        "items",
        {
            "item_code": item_code,
            "qty": quantity,
            "rate": unit_price,
            "warehouse": setting.warehouse,
            "delivery_date": so.delivery_date,
            "description": line_item.get("label", ""),
        },
    )


def _add_shipping_item(so: "frappe.Document", line_item: dict[str, Any], setting) -> None:
    """Add shipping as a line item.

    Supports both B2B (net prices) and B2C (gross prices) modes.
    """
    if not setting.shipping_item:
        return

    # Handle B2B (net) vs B2C (gross) mode for shipping
    use_net_prices = getattr(setting, 'import_prices_as_net', True)
    default_tax_rate = flt(getattr(setting, 'default_tax_rate', 19.0))

    if use_net_prices:
        # B2B mode: Convert gross to net price
        shipping_price = flt(get_net_unit_price_from_line_item(line_item, default_tax_rate))
    else:
        # B2C mode: Use gross price as-is
        shipping_price = flt(line_item.get("unitPrice", 0))

    if shipping_price > 0:
        so.append(
            "items",
            {
                "item_code": setting.shipping_item,
                "qty": 1,
                "rate": shipping_price,
                "warehouse": setting.warehouse,
                "delivery_date": so.delivery_date,
            },
        )


def _add_discount_item(so: "frappe.Document", line_item: dict[str, Any], setting) -> None:
    """
    Add a discount/promotion as a negative line item.

    Shopware promotions are stored as separate line items with type "promotion" or "discount"
    and typically have negative prices. This creates a corresponding negative line item
    in ERPNext to reflect the discount.

    Args:
        so: Sales Order document
        line_item: Shopware discount/promotion line item data
        setting: Shopware Setting document
    """
    # Get discount item from settings or use a default
    discount_item = getattr(setting, 'discount_item', None)
    if not discount_item:
        # Try to find or create a generic discount item
        discount_item = frappe.db.get_value("Item", {"item_code": "DISCOUNT"}, "name")
        if not discount_item:
            # Log and skip if no discount item configured
            frappe.log_error(
                f"Discount line item found in Shopware order but no discount item configured. "
                f"Discount label: {line_item.get('label')}, Amount: {line_item.get('totalPrice')}",
                "Shopware Discount Handling"
            )
            return

    # Get discount price - discounts typically have negative total price
    # Use totalPrice instead of unitPrice for promotions as it may be more accurate
    discount_price = flt(line_item.get("totalPrice", 0))

    # If not negative (Shopware may already store as negative), make it negative
    if discount_price > 0:
        discount_price = -discount_price

    # Handle B2B (net) vs B2C (gross) mode for discounts
    use_net_prices = getattr(setting, 'import_prices_as_net', True)
    default_tax_rate = flt(getattr(setting, 'default_tax_rate', 19.0))

    if use_net_prices and discount_price != 0:
        # B2B mode: Convert gross to net price
        tax_rate = get_tax_rate_from_line_item(line_item, default_tax_rate)
        discount_price = convert_gross_to_net(discount_price, tax_rate)

    # Get discount label/description
    label = line_item.get("label", "") or line_item.get("description", "") or "Rabatt"

    if discount_price != 0:
        so.append(
            "items",
            {
                "item_code": discount_item,
                "qty": 1,
                "rate": discount_price,  # Negative value for discount
                "warehouse": setting.warehouse,
                "delivery_date": so.delivery_date,
                "description": label,
            },
        )


def add_order_taxes(so: "frappe.Document", order_data: dict[str, Any], setting) -> None:
    """
    Add taxes to the Sales Order.

    When import_prices_as_net is enabled (B2B mode), taxes are added as "On Net Total"
    with the tax rate as percentage. This ensures all items are taxed correctly.

    When import_prices_as_net is disabled (B2C mode), taxes are added as "Actual"
    with the exact amount from Shopware (since prices already include tax).

    Args:
        so: Sales Order document
        order_data: Shopware order data
        setting: Shopware Setting document
    """
    use_net_prices = getattr(setting, 'import_prices_as_net', True)

    # Collect unique tax rates and their amounts from line items
    tax_data = {}  # {tax_rate: {"amount": 0, "account": None}}

    for line_item in order_data.get("lineItems", []):
        price = line_item.get("price", {})
        calculated_taxes = price.get("calculatedTaxes", [])

        for tax in calculated_taxes:
            tax_rate = flt(tax.get("taxRate", 0))
            tax_amount = flt(tax.get("tax", 0))

            if tax_rate not in tax_data:
                tax_data[tax_rate] = {"amount": 0, "account": None}
            tax_data[tax_rate]["amount"] += tax_amount

    # Add shipping taxes
    for delivery in order_data.get("deliveries", []):
        shipping_costs = delivery.get("shippingCosts", {})
        calculated_taxes = shipping_costs.get("calculatedTaxes", [])

        for tax in calculated_taxes:
            tax_rate = flt(tax.get("taxRate", 0))
            tax_amount = flt(tax.get("tax", 0))

            if tax_rate not in tax_data:
                tax_data[tax_rate] = {"amount": 0, "account": None}
            tax_data[tax_rate]["amount"] += tax_amount

    # Find mapped accounts for each tax rate
    for tax_rate in tax_data:
        for tax_mapping in setting.taxes:
            if flt(tax_mapping.shopware_tax) == tax_rate:
                tax_data[tax_rate]["account"] = tax_mapping.tax_account
                break

        if not tax_data[tax_rate]["account"]:
            tax_data[tax_rate]["account"] = setting.default_sales_tax_account

    # Create tax entries
    for tax_rate, data in tax_data.items():
        if data["amount"] <= 0:
            continue

        if not data["account"]:
            continue

        if use_net_prices:
            # B2B mode: Use "On Net Total" with tax rate as percentage
            # This ensures all items are taxed correctly with the same rate
            so.append(
                "taxes",
                {
                    "charge_type": "On Net Total",
                    "account_head": data["account"],
                    "rate": tax_rate,
                    "description": f"VAT {tax_rate}%",
                    "cost_center": setting.cost_center,
                    "included_in_print_rate": 0,
                },
            )
        else:
            # B2C mode: Use "Actual" with exact amount (prices include tax)
            so.append(
                "taxes",
                {
                    "charge_type": "Actual",
                    "account_head": data["account"],
                    "tax_amount": data["amount"],
                    "description": f"VAT {tax_rate}%",
                    "cost_center": setting.cost_center,
                },
            )


def create_delivery_note(sales_order: str, delivery_data: dict[str, Any], setting) -> str | None:
    """
    Create Delivery Note from Shopware shipment data.

    Args:
        sales_order: ERPNext Sales Order name
        delivery_data: Shopware delivery object
        setting: Shopware Setting document

    Returns:
        Delivery Note name if created
    """
    try:
        # Check if DN already exists
        existing = frappe.db.get_value(
            "Delivery Note", {"shopware_delivery_id": delivery_data.get("id")}, "name"
        )
        if existing:
            return existing

        # Create Delivery Note from Sales Order
        from erpnext.selling.doctype.sales_order.sales_order import make_delivery_note

        dn = make_delivery_note(sales_order)
        dn.naming_series = setting.delivery_note_series
        dn.shopware_delivery_id = delivery_data.get("id")

        # Set tracking number if available
        tracking = delivery_data.get("trackingCodes", [])
        if tracking:
            dn.lr_no = tracking[0] if isinstance(tracking, list) else tracking

        dn.insert(ignore_permissions=True)
        dn.submit()

        return dn.name

    except Exception as e:
        frappe.log_error(f"Failed to create Delivery Note for {sales_order}: {e}")
        return None


def create_sales_invoice(sales_order: str, transaction_data: dict[str, Any], setting) -> str | None:
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
        # Check if SI already exists
        existing = frappe.db.get_value(
            "Sales Invoice", {"shopware_transaction_id": transaction_data.get("id")}, "name"
        )
        if existing:
            return existing

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
        frappe.log_error(f"Failed to create Sales Invoice for {sales_order}: {e}")
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
        frappe.log_error(
            f"Failed to create Payment Entry for {sales_invoice.name}: {e}",
            "Shopware Payment Entry Error"
        )
        return None


@frappe.whitelist()
def sync_orders_from_shopware(
    from_date: str = None,
    to_date: str = None,
    limit: int = 100
) -> Dict[str, int]:
    """
    Manually sync orders from Shopware.

    Args:
        from_date: Start date for order filter (YYYY-MM-DD)
        to_date: End date for order filter (YYYY-MM-DD)
        limit: Maximum number of orders to sync

    Returns:
        dict: Sync results
    """
    client = get_shopware_client()

    criteria = Criteria(limit=int(limit))

    # Add date filters if provided
    if from_date or to_date:
        date_filter = {}
        if from_date:
            date_filter["gte"] = f"{from_date}T00:00:00.000Z"
        if to_date:
            date_filter["lte"] = f"{to_date}T23:59:59.999Z"
        criteria.filter.append(RangeFilter("orderDateTime", date_filter))

    # Add associations
    criteria.associations["lineItems"] = Criteria()
    criteria.associations["orderCustomer"] = Criteria()
    criteria.associations["orderCustomer"].associations["salutation"] = Criteria()
    criteria.associations["billingAddress"] = Criteria()
    criteria.associations["billingAddress"].associations["country"] = Criteria()
    criteria.associations["billingAddress"].associations["countryState"] = Criteria()
    criteria.associations["deliveries"] = Criteria()
    criteria.associations["deliveries"].associations["shippingOrderAddress"] = Criteria()
    criteria.associations["deliveries"].associations["shippingOrderAddress"].associations["country"] = Criteria()
    criteria.associations["deliveries"].associations["shippingOrderAddress"].associations["countryState"] = Criteria()
    criteria.associations["transactions"] = Criteria()
    criteria.associations["currency"] = Criteria()

    response = client.request_post("search/order", criteria)
    orders = response.get("data", [])

    synced = 0
    errors = 0

    for order_data in orders:
        try:
            order_id = order_data.get("id")

            # Check if already synced
            existing = frappe.db.get_value("Sales Order", {"shopware_order_id": order_id}, "name")
            if not existing:
                create_sales_order(order_data)
                synced += 1
        except Exception as e:
            errors += 1
            frappe.log_error(
                f"Failed to sync order {order_data.get('orderNumber')}: {e}",
                "Shopware Order Sync Error"
            )

    return {
        "synced": synced,
        "errors": errors,
        "total": len(orders),
    }


def scheduled_order_sync():
    """
    Scheduled job to sync orders from Shopware.
    Respects the order_sync_frequency setting.
    Called every minute by scheduler, but only runs if enough time has passed.
    """
    from frappe.utils import now_datetime, time_diff_in_seconds

    setting = frappe.get_doc(SETTING_DOCTYPE)

    if not setting.is_enabled():
        return

    # Get frequency in minutes (default 60)
    frequency = int(setting.order_sync_frequency or 60)
    frequency_seconds = frequency * 60

    # Check if enough time has passed since last sync
    if setting.last_order_sync:
        elapsed = time_diff_in_seconds(now_datetime(), setting.last_order_sync)
        if elapsed < frequency_seconds:
            return  # Not time yet

    try:
        # Sync orders from the last 24 hours
        from frappe.utils import add_days, nowdate

        result = sync_orders_from_shopware(
            from_date=add_days(nowdate(), -1),
            limit=100
        )

        # Update last sync time
        frappe.db.set_single_value(SETTING_DOCTYPE, "last_order_sync", now_datetime())
        frappe.db.commit()

        if result.get("synced", 0) > 0:
            frappe.logger("shopware6").info(
                f"Order sync completed: {result['synced']} synced, {result['errors']} errors"
            )

    except Exception as e:
        frappe.log_error(f"Scheduled order sync failed: {e}", "Shopware Order Sync")


def sync_old_orders():
    """
    Scheduled job to sync old orders from Shopware.
    Similar to Shopify's sync_old_orders functionality.
    This allows syncing orders that existed before the integration was set up.
    Called hourly by scheduler.
    """
    from frappe.utils import cint, get_datetime

    setting = frappe.get_cached_doc(SETTING_DOCTYPE)

    if not cint(setting.sync_old_orders):
        return

    if not setting.is_enabled():
        return

    if not setting.old_orders_from or not setting.old_orders_to:
        frappe.log_error(
            "old_orders_from and old_orders_to are required for syncing old orders", "Shopware Order Sync"
        )
        return

    try:
        # Convert to ISO format for API
        from_time = get_datetime(setting.old_orders_from).astimezone().isoformat()
        to_time = get_datetime(setting.old_orders_to).astimezone().isoformat()

        # Get client directly - don't use decorator on scheduled jobs
        client = get_shopware_client()

        # Use pagination to fetch all orders in the date range
        criteria = Criteria(limit=100)

        # Add date filter
        criteria.filter.append(RangeFilter("orderDateTime", {"gte": from_time, "lte": to_time}))

        # Add associations
        criteria.associations["lineItems"] = Criteria()
        criteria.associations["orderCustomer"] = Criteria()
        criteria.associations["orderCustomer"].associations["salutation"] = Criteria()
        criteria.associations["billingAddress"] = Criteria()
        criteria.associations["billingAddress"].associations["country"] = Criteria()
        criteria.associations["billingAddress"].associations["countryState"] = Criteria()
        criteria.associations["deliveries"] = Criteria()
        criteria.associations["deliveries"].associations["shippingOrderAddress"] = Criteria()
        criteria.associations["deliveries"].associations["shippingOrderAddress"].associations["country"] = Criteria()
        criteria.associations["deliveries"].associations["shippingOrderAddress"].associations["countryState"] = Criteria()
        criteria.associations["deliveries"].associations["shippingMethod"] = Criteria()
        criteria.associations["transactions"] = Criteria()
        criteria.associations["transactions"].associations["paymentMethod"] = Criteria()
        criteria.associations["currency"] = Criteria()

        response = client.request_post("search/order", criteria)
        orders = response.get("data", [])

        synced = 0
        skipped = 0
        errors = 0

        for order_data in orders:
            try:
                order_id = order_data.get("id")

                # Check if already synced
                existing = frappe.db.get_value("Sales Order", {"shopware_order_id": order_id}, "name")
                if existing:
                    skipped += 1
                    continue

                # Create order using the standard function
                create_sales_order(order_data)
                synced += 1

            except Exception as e:
                errors += 1
                frappe.log_error(
                    f"Failed to sync old order {order_data.get('orderNumber')}: {e}",
                    "Shopware Old Order Sync Error",
                )

        # Disable the flag after successful sync (consistent with Shopify implementation)
        if errors == 0:
            setting = frappe.get_doc(SETTING_DOCTYPE)
            setting.sync_old_orders = 0
            setting.save()

        frappe.logger("shopware6").info(
            f"Old order sync completed: {synced} synced, {skipped} skipped, {errors} errors"
        )

    except Exception as e:
        frappe.log_error(f"Scheduled old order sync failed: {e}", "Shopware Old Order Sync")
