"""
Shopware 6 Order Sync

Main ShopwareOrder class and order synchronization functions.
Follows the Shopify integration pattern.
"""

from typing import Any

import frappe
from frappe import _
from frappe.utils import getdate, nowdate
from lib_shopware6_api_base import Criteria, Shopware6AdminAPIClientBase

from ecommerce_integrations.ecommerce_integrations.ecommerce_custom_fields import STOREFRONT_SHOPWARE
from ecommerce_integrations.shopware6.connection import temp_shopware_session
from ecommerce_integrations.shopware6.constants import (
    ORDER_SYNC_LOCK_TIMEOUT,
    SETTING_DOCTYPE,
    STATUS_SYNC_ENQUEUE_TIMEOUT,
)
from ecommerce_integrations.shopware6.customer import (
    ensure_customer_has_address,
    get_customer_from_shopware_order,
)
from ecommerce_integrations.shopware6.order.delivery_handler import create_delivery_note
from ecommerce_integrations.shopware6.order.line_item_handler import add_order_item, add_shipping_costs
from ecommerce_integrations.shopware6.order.order_mapper import (
    calculate_delivery_date,
    extract_order_currency,
    get_payment_method_info,
    get_tax_template,
)
from ecommerce_integrations.shopware6.order.payment_handler import (
    create_sales_invoice,
)
from ecommerce_integrations.shopware6.order.service_items import process_checkout_fields
from ecommerce_integrations.shopware6.order.tax_handler import add_order_taxes
from ecommerce_integrations.shopware6.product import create_items_if_not_exist
from ecommerce_integrations.shopware6.utils import (
    create_shopware_log,
    format_shopware_datetime,
    get_logger,
    update_shopware_log,
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

    def __init__(self, order_id: str, sales_order_name: str | None = None):
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
            frappe.throw(_("Shopware-Integration ist nicht aktiviert."))

    def _get_existing_sales_order(self) -> str | None:
        """Check if order is already synced."""
        return frappe.db.get_value("Sales Order", {"shopware_order_id": self.order_id}, "name")

    def is_synced(self) -> bool:
        """Check if order is already synced to ERPNext."""
        return bool(self.sales_order_name)

    @temp_shopware_session
    def sync(self, client) -> str | None:
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
def sync_order_by_id(
    client: Shopware6AdminAPIClientBase,
    order_id: str,
    webhook_custom_fields: dict[str, Any] | None = None,
) -> str | None:
    """
    Sync a specific order from Shopware by ID.

    Args:
        client: Shopware API client
        order_id: Shopware order ID
        webhook_custom_fields: Optional customFields dict received with the
            webhook event. Merged into ``order_data.customFields`` (webhook
            wins) before mapping resolution, so checkout-field mappings work
            even when Shopware returns ``customFields: null`` on the order
            entity itself.

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
    orders = response.data or []

    if not orders:
        frappe.throw(_("Bestellung nicht in Shopware gefunden: {0}").format(order_id))

    order_data = orders[0]
    if webhook_custom_fields:
        merged = dict(order_data.get("customFields") or {})
        merged.update(webhook_custom_fields)
        order_data["customFields"] = merged
    return create_sales_order(order_data)


def _build_order_fetch_criteria(order_id: str) -> Criteria:
    """Build criteria for fetching a single order with all associations."""
    criteria = Criteria(ids=[order_id])
    criteria.associations["stateMachineState"] = Criteria()
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
    criteria.associations["transactions"].associations["stateMachineState"] = Criteria()
    criteria.associations["currency"] = Criteria()
    # Multi-Storefront: Fetch sales channel info
    criteria.associations["salesChannel"] = Criteria()
    return criteria


# Map a Shopware payment-handler identifier to a normalised PSP provider name.
# The handler is the stable signal (the human payment-method name is operator
# editable / localised, e.g. "Kredit- oder Debitkarte" is still PayPal's ACDC).
_PSP_HANDLER_MARKERS = (
    ("paypal", "PayPal"),
    ("stripe", "Stripe"),
    ("klarna", "Klarna"),
    ("mollie", "Mollie"),
    ("unzer", "Unzer"),
)


def _normalise_psp_provider(handler: str, method_name: str | None) -> str:
    h = (handler or "").lower()
    for marker, name in _PSP_HANDLER_MARKERS:
        if marker in h:
            return name
    return method_name or "Unknown"


def _capture_psp_reference(so, order_data: dict[str, Any]) -> None:
    """Store provider + PSP transaction id on the Sales Order for later
    fee/payout reconciliation. Shopware does not persist the PSP fee, but the
    order transaction's ``customFields`` carry the provider's ids (PayPal:
    ``swag_paypal_resource_id`` = capture id, ``swag_paypal_order_id``; a Stripe
    or Klarna plugin exposes its own keys). Best-effort: never raises."""
    try:
        txns = order_data.get("transactions") or []
        if not txns:
            return
        txn = txns[0]
        cf = txn.get("customFields") or {}
        pm = txn.get("paymentMethod") or {}
        handler = pm.get("handlerIdentifier") or ""
        provider = _normalise_psp_provider(handler, pm.get("name"))

        txn_id = (
            cf.get("swag_paypal_resource_id")       # PayPal capture id (fee lookup)
            or cf.get("stripe_payment_intent_id")
            or cf.get("stripe_charge_id")
            or cf.get("klarna_order_id")
            or txn.get("id")                        # fallback: Shopware transaction id
        )
        order_ref = (
            cf.get("swag_paypal_order_id")
            or cf.get("klarna_order_id")
            or order_data.get("id")
        )

        def _set(field, value):
            if value is not None and so.meta.has_field(field):
                so.set(field, value)

        _set("psp_provider", provider)
        _set("psp_transaction_id", txn_id)
        _set("psp_order_reference", order_ref)
        _set("psp_payment_handler", handler)
        sb = cf.get("swag_paypal_is_sandbox")
        if sb is not None:
            _set("psp_is_sandbox", 1 if sb else 0)
        # Captured gross from the transaction itself, so the receipt can be booked
        # to the exact PSP-captured amount (partial captures / rounding) rather
        # than the full invoice outstanding. Best-effort: only when present.
        captured = (txn.get("amount") or {}).get("totalPrice")
        if captured is not None:
            _set("psp_captured_amount", captured)
    except Exception:
        frappe.logger("shopware6").warning(
            f"PSP reference capture failed for order {order_data.get('id')}",
            exc_info=True,
        )


def _insert_sales_order(so, desired_name: str) -> None:
    """Insert ``so`` naming it ``desired_name`` (the Shopware order number).

    Falls back to a disambiguated name on a collision (e.g. a manually
    created Sales Order already used that same plain number) instead of
    failing the whole sync — the Shopware IDs on the doc (shopware_order_id/
    shopware_order_number) still make the order findable either way.
    """
    try:
        so.insert(ignore_permissions=True, set_name=desired_name)
    except frappe.DuplicateEntryError:
        so.insert(ignore_permissions=True, set_name=f"{desired_name}-SW")


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

    # Skip cancelled orders - they should not be created in ERPNext
    order_state = (order_data.get("stateMachineState") or {}).get("technicalName", "")
    if order_state == "cancelled":
        frappe.logger("shopware6").info(f"Skipping cancelled order {order_number} ({order_id})")
        return None

    # Check if already synced
    existing = frappe.db.get_value("Sales Order", {"shopware_order_id": order_id}, "name")
    if existing:
        frappe.logger("shopware6").info(f"Order {order_id} already exists as {existing}, skipping creation")
        return existing

    # Sync items first
    create_items_if_not_exist(order_data)

    # Get or create customer
    # Wrap in try/except to handle Chatwoot sync errors that shouldn't crash order import
    try:
        customer = get_customer_from_shopware_order(order_data)
    except Exception as e:
        # Log the error but try to continue - customer might have been created before the hook failed
        frappe.logger("shopware6").warning(f"Customer sync had an error (will try to continue): {e}")
        # Try to find the customer anyway
        order_customer = order_data.get("orderCustomer", {})
        shopware_customer_id = order_customer.get("customerId") or order_customer.get("customer", {}).get("id")
        if shopware_customer_id:
            customer = frappe.db.get_value("Customer", {"shopware_customer_id": shopware_customer_id}, "name")
        if not customer:
            # Re-raise if we can't find the customer
            raise

    # Ensure customer has address (important for PayPal orders where address may be missing initially)
    ensure_customer_has_address(customer, order_data)

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
        calculate_delivery_date(order_date, order_data.get("lineItems") or [])
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
    so.ecommerce_source = STOREFRONT_SHOPWARE
    so.ecommerce_sales_channel_id = sales_channel_id
    so.ecommerce_sales_channel_name = sales_channel_name

    # Extract and set payment method info
    payment_method_name, erpnext_mode, payment_status = get_payment_method_info(order_data, setting)
    so.shopware_payment_method = payment_method_name
    so.shopware_payment_status = payment_status

    # PSP reference for fee/payout reconciliation. Shopware does NOT persist the
    # PSP fee, but the order transaction carries the provider's transaction id in
    # its ``customFields`` (e.g. ``swag_paypal_resource_id`` = PayPal capture id).
    # Store provider + transaction id so the accounting side can later pull the
    # exact fee from the provider API and book it gross (§246 HGB).
    _capture_psp_reference(so, order_data)

    if erpnext_mode:
        so.mode_of_payment = erpnext_mode
        if hasattr(so, 'shopware_erpnext_mode_of_payment'):
            so.shopware_erpnext_mode_of_payment = erpnext_mode

    # Process all configured checkout custom fields from Shopware Settings
    process_checkout_fields(so, setting, order_data, customer)

    # Extract taxStatus from Shopware order to determine price handling
    # - "net": B2B order, prices are already net
    # - "gross": B2C order, prices include tax
    # - "tax-free": No tax applied
    tax_status = order_data.get("taxStatus", "gross")
    # Also check price object for taxStatus (fallback)
    if not tax_status or tax_status not in ("net", "gross", "tax-free"):
        price_data = order_data.get("price", {})
        tax_status = price_data.get("taxStatus", "gross")

    # Add line items with correct price handling based on taxStatus
    for line_item in (order_data.get("lineItems") or []):
        add_order_item(so, line_item, setting, tax_status)


    # Add shipping costs as line item if configured
    add_shipping_costs(so, order_data, setting, tax_status)

    # Determine and set tax template based on delivery country and B2B status
    # This is important for proper VAT classification in reporting
    tax_template = get_tax_template(order_data, setting.company)
    if tax_template:
        so.taxes_and_charges = tax_template

    # Add taxes with correct included_in_print_rate based on taxStatus
    add_order_taxes(so, order_data, setting, tax_status)

    # Set cost center
    if setting.cost_center:
        for item in so.items:
            item.cost_center = setting.cost_center

    # Check if Sales Order Approval workflow is active
    workflow_active = frappe.db.exists("Workflow", {"document_type": "Sales Order", "is_active": 1})

    # Always name the Sales Order after the Shopware order number so it's
    # directly recognisable/searchable as "the order" — independent of
    # whatever Sales Order's Auto Name is set to (naming_series or Prompt;
    # some sites set Prompt precisely to preserve source-system order
    # numbers as the document name, which otherwise fails with "Please
    # set the document name").
    desired_name = order_number or order_id

    try:
        _validate_item_group_hierarchy(so)

        if workflow_active:
            # Set workflow state to Pending Approval (do NOT submit - workflow controls this)
            so.workflow_state = "Pending Approval"
            _insert_sales_order(so, desired_name)
            # Note: Delivery Note and Sales Invoice will be created when order is approved
        else:
            # No workflow - use original behavior (auto-submit)
            _insert_sales_order(so, desired_name)
            so.submit()

            # Create Delivery Note if configured and order is shipped
            if setting.sync_delivery_note:
                _create_delivery_note_if_shipped(so.name, order_data, setting)

            # Create Sales Invoice if configured and order is paid
            if setting.sync_sales_invoice:
                _create_invoice_if_paid(so.name, order_data, setting)

        frappe.db.commit()

        # Note: do NOT pass `request_data` here. When this runs inside the
        # webhook flow (frappe.flags.request_id set), `create_log` reuses the
        # existing log and would clobber the original webhook payload with
        # this 2-key summary — making post-mortem diagnosis impossible. The
        # original payload (including customFields) is far more valuable.
        create_shopware_log(
            status="Success",
            method="create_sales_order",
            message=f"Created Sales Order {so.name} from Shopware order {order_number}",
        )

        # Schedule async verification of payment status
        # This handles the race condition where payment webhook arrived before order was created
        frappe.enqueue(
            "ecommerce_integrations.shopware6.order.payment_handler.verify_payment_status_from_shopware",
            order_id=order_id,
            sales_order_name=so.name,
            queue="short",
            timeout=STATUS_SYNC_ENQUEUE_TIMEOUT,
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


def _validate_item_group_hierarchy(so: "frappe.Document") -> None:
    """Fail fast if an item's Item Group tree is cyclic to avoid ERPNext infinite loops."""
    for item in so.get("items") or []:
        item_code = item.get("item_code")
        if not item_code:
            continue

        item_group = frappe.db.get_value("Item", item_code, "item_group")
        seen = set()
        chain = []
        hops = 0

        while item_group:
            chain.append(item_group)

            if item_group in seen:
                frappe.throw(
                    _(
                        "Invalid Item Group hierarchy for item {0}: cycle detected in {1}"
                    ).format(item_code, " -> ".join(chain))
                )

            seen.add(item_group)
            parent = frappe.db.get_value("Item Group", item_group, "parent_item_group")

            if parent == item_group:
                # Root node in ERPNext's nested set points to itself - this is normal
                break

            item_group = parent
            hops += 1
            if hops > 100:
                frappe.throw(
                    _("Invalid Item Group hierarchy for item {0}: maximum depth exceeded").format(item_code)
                )



def _create_delivery_note_if_shipped(
    sales_order: str,
    order_data: dict[str, Any],
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
    order_data: dict[str, Any],
    setting
) -> None:
    """Create Sales Invoice if order is paid or requires prepayment invoice."""
    transactions = order_data.get("transactions") or []
    for transaction in transactions:
        state = transaction.get("stateMachineState") or {}
        if state.get("technicalName") in ["paid", "completed"]:
            create_sales_invoice(sales_order, transaction, setting)
            break
    else:
        # Create invoice for unpaid methods that require an invoice up-front
        _, erpnext_mode, _ = get_payment_method_info(order_data, setting)
        unpaid_invoice_modes = {"Vorkasse", "Rechnung", "Nachnahme", "Bargeld"}
        if erpnext_mode in unpaid_invoice_modes:
            transaction = transactions[0] if transactions else {}
            create_sales_invoice(sales_order, transaction, setting)


def sync_order_from_webhook(payload: dict[str, Any], request_id: str | None = None):
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

    # Add a lock to prevent concurrent processing of the same order
    order_id = _extract_order_id_from_payload(payload)
    if not order_id:
        if request_id:
            update_shopware_log(request_id, status="Error", message="No order ID in webhook payload")
        return

    lock = frappe.cache().lock(f"shopware_sync_order_{order_id}", timeout=ORDER_SYNC_LOCK_TIMEOUT)
    if not lock.acquire(blocking=False):
        if request_id:
            update_shopware_log(request_id, status="Skipped", message=f"Sync already in progress for order {order_id}")
        return

    try:
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
                # New order - sync it. We forward the webhook's customFields
                # so all `Shopware Setting.checkout_fields` mappings see them
                # — Shopware's order entity often returns `customFields: null`
                # on freshly-written orders, but the webhook payload is
                # authoritative (the storefront subscriber reads them
                # in-flight at write-time).
                sync_order_by_id(order_id, webhook_custom_fields=webhook_custom_fields)
                if request_id:
                    update_shopware_log(request_id, status="Success", message=f"Synced order {order_id}")
        else:
            if request_id:
                update_shopware_log(request_id, status="Error", message="No order ID in webhook payload")

    except Exception as e:
        if request_id:
            update_shopware_log(request_id, status="Error", exception=str(e))
        raise
    finally:
        lock.release()


def _extract_order_id_from_payload(payload: dict[str, Any]) -> str | None:
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
    webhook_custom_fields: dict[str, Any],
    request_id: str | None = None
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

        updates_made = []
        so_meta = frappe.get_meta("Sales Order")
        customer_meta = frappe.get_meta("Customer") if so.customer else None

        for row in (setting.get("checkout_fields") or []):
            if not row.enabled:
                continue

            field_names = [n.strip() for n in (row.source_field_names or "").split(",") if n.strip()]
            value = None
            for name in field_names:
                if name in webhook_custom_fields:
                    value = webhook_custom_fields[name]
                    break

            if value is None:
                continue

            if row.mapping_type == "Sales Order Field" and row.target_field:
                if so_meta.has_field(row.target_field) and so.get(row.target_field) != value:
                    frappe.db.set_value("Sales Order", sales_order_name, row.target_field, value)
                    updates_made.append(f"{row.target_field}={value}")
            elif row.mapping_type == "Customer Update" and row.target_field and so.customer:
                if customer_meta and customer_meta.has_field(row.target_field):
                    frappe.db.set_value("Customer", so.customer, row.target_field, value, update_modified=False)
                    updates_made.append(f"customer.{row.target_field}={value}")

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
        get_logger().error(f"Failed to update custom fields for {sales_order_name}: {e}", persist=True)
        if request_id:
            update_shopware_log(request_id, status="Error", exception=str(e))


def update_order_status(payload: dict[str, Any], request_id: str | None = None):
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

                        # If payment is now "Paid" and auto-invoice is enabled, trigger verification
                        if erpnext_status == "Paid":
                            from ecommerce_integrations.shopware6.order.payment_handler import (
                                verify_payment_status_from_shopware,
                            )
                            verify_payment_status_from_shopware(order_id, sales_order)

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
