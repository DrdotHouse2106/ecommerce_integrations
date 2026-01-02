"""
Shopware 6 Webhook Handler

Routes webhook events to appropriate handlers.
"""

from typing import Any, Dict, Optional

import frappe
from frappe import _

from ecommerce_integrations.shopware6.constants import SETTING_DOCTYPE
from ecommerce_integrations.shopware6.utils import create_shopware_log, get_logger
from ecommerce_integrations.shopware6.validators import ShopwareDataValidator


# Webhook event type to handler mapping
EVENT_HANDLERS = {
    # Order events
    "order.placed": "handle_order_placed",
    "order.written": "handle_order_written",
    "order.deleted": "handle_order_deleted",
    "checkout.order.placed": "handle_order_placed",

    # Order state events
    "order.state_machine_state.changed": "handle_order_status_changed",
    "order_transaction.state_machine_state.changed": "handle_payment_status_changed",
    "order_delivery.state_machine_state.changed": "handle_delivery_status_changed",

    # Product events
    "product.written": "handle_product_written",
    "product.deleted": "handle_product_deleted",

    # Customer events
    "customer.written": "handle_customer_written",
    "customer.deleted": "handle_customer_deleted",

    # Inventory events
    "product_stock.written": "handle_stock_changed",
}


class WebhookHandler:
    """
    Handler for Shopware webhook events.

    Routes events to appropriate handlers and manages logging.
    """

    def __init__(self):
        self.setting = frappe.get_cached_doc(SETTING_DOCTYPE)

    def handle(
        self,
        event_type: str,
        entity_id: str,
        payload: Dict[str, Any],
        request_id: str = None
    ) -> bool:
        """
        Handle a webhook event.

        Args:
            event_type: Type of event
            entity_id: Entity ID
            payload: Webhook payload
            request_id: Log entry ID for tracking

        Returns:
            True if handled successfully
        """
        if not self.setting.is_enabled():
            frappe.logger("shopware6").debug(
                f"Shopware integration disabled, skipping webhook: {event_type}"
            )
            return False

        # Validate payload
        is_valid, errors = ShopwareDataValidator.validate_webhook_payload(payload)
        if not is_valid:
            get_logger().error("Invalid webhook payload for {event_type}", persist=True)
            return False

        # Find handler
        handler_name = EVENT_HANDLERS.get(event_type)
        if not handler_name:
            # Unknown event type - log but don't fail
            frappe.logger("shopware6").debug(
                f"No handler for event type: {event_type}"
            )
            return True

        # Get handler method
        handler = getattr(self, handler_name, None)
        if not handler:
            get_logger().error("Handler method not found", persist=True)
            return False

        try:
            return handler(entity_id, payload, request_id)
        except Exception as e:
            get_logger().error("Error in webhook handler {handler_name}", persist=True)
            if request_id:
                from ecommerce_integrations.shopware6.utils import update_shopware_log
                update_shopware_log(request_id, status="Error", exception=str(e))
            return False

    # Order Handlers

    def handle_order_placed(
        self,
        order_id: str,
        payload: Dict[str, Any],
        request_id: str = None
    ) -> bool:
        """Handle new order placement."""
        from ecommerce_integrations.shopware6.order import sync_order_from_webhook

        sync_order_from_webhook(payload, request_id)
        return True

    def handle_order_written(
        self,
        order_id: str,
        payload: Dict[str, Any],
        request_id: str = None
    ) -> bool:
        """Handle order update."""
        from ecommerce_integrations.shopware6.order import sync_order_from_webhook

        # Mark as update
        if "data" not in payload:
            payload["data"] = {}
        payload["data"]["isUpdate"] = True

        sync_order_from_webhook(payload, request_id)
        return True

    def handle_order_deleted(
        self,
        order_id: str,
        payload: Dict[str, Any],
        request_id: str = None
    ) -> bool:
        """Handle order deletion."""
        # Find linked Sales Order
        sales_order = frappe.db.get_value(
            "Sales Order",
            {"shopware_order_id": order_id},
            "name"
        )

        if sales_order:
            frappe.logger("shopware6").warning(
                f"Order {order_id} deleted in Shopware, linked Sales Order: {sales_order}"
            )
            # Don't automatically delete - just log
            create_shopware_log(
                status="Warning",
                method="handle_order_deleted",
                message=f"Order {order_id} deleted in Shopware. Sales Order {sales_order} still exists.",
            )

        return True

    def handle_order_status_changed(
        self,
        order_id: str,
        payload: Dict[str, Any],
        request_id: str = None
    ) -> bool:
        """Handle order status change."""
        from ecommerce_integrations.shopware6.order import update_order_status

        update_order_status(payload, request_id)
        return True

    def handle_payment_status_changed(
        self,
        transaction_id: str,
        payload: Dict[str, Any],
        request_id: str = None
    ) -> bool:
        """Handle payment status change."""
        from ecommerce_integrations.shopware6.order import update_order_status

        update_order_status(payload, request_id)
        return True

    def handle_delivery_status_changed(
        self,
        delivery_id: str,
        payload: Dict[str, Any],
        request_id: str = None
    ) -> bool:
        """Handle delivery status change."""
        # Get order ID from payload
        entity = payload.get("payload", {}).get("entity", {})
        order_id = entity.get("orderId")

        if order_id:
            sales_order = frappe.db.get_value(
                "Sales Order",
                {"shopware_order_id": order_id},
                "name"
            )

            if sales_order:
                state = entity.get("stateMachineState", {})
                if state.get("technicalName") in ["shipped", "delivered"]:
                    # Create Delivery Note if not exists
                    from ecommerce_integrations.shopware6.order import create_delivery_note

                    setting = frappe.get_doc(SETTING_DOCTYPE)
                    if setting.sync_delivery_note:
                        create_delivery_note(sales_order, entity, setting)

        return True

    # Product Handlers

    def handle_product_written(
        self,
        product_id: str,
        payload: Dict[str, Any],
        request_id: str = None
    ) -> bool:
        """Handle product create/update."""
        if not self.setting.sync_products_from_shopware:
            return True

        from ecommerce_integrations.shopware6.product import ShopwareProduct

        product = ShopwareProduct(product_id)
        product.sync_product()
        return True

    def handle_product_deleted(
        self,
        product_id: str,
        payload: Dict[str, Any],
        request_id: str = None
    ) -> bool:
        """Handle product deletion."""
        # Find linked ERPNext Item
        ecommerce_item = frappe.db.get_value(
            "Ecommerce Item",
            {"integration_item_code": product_id, "integration": "Shopware6"},
            ["erpnext_item_code", "name"],
            as_dict=True
        )

        if ecommerce_item:
            frappe.logger("shopware6").warning(
                f"Product {product_id} deleted in Shopware, linked Item: {ecommerce_item.erpnext_item_code}"
            )
            # Don't automatically delete - just remove the link
            frappe.delete_doc("Ecommerce Item", ecommerce_item.name, ignore_permissions=True)

        return True

    # Customer Handlers

    def handle_customer_written(
        self,
        customer_id: str,
        payload: Dict[str, Any],
        request_id: str = None
    ) -> bool:
        """Handle customer create/update."""
        if not self.setting.sync_customers_from_shopware:
            return True

        from ecommerce_integrations.shopware6.customer import ShopwareCustomer

        customer = ShopwareCustomer(customer_id=customer_id)
        customer.sync()
        return True

    def handle_customer_deleted(
        self,
        customer_id: str,
        payload: Dict[str, Any],
        request_id: str = None
    ) -> bool:
        """Handle customer deletion."""
        # Find linked ERPNext Customer
        customer = frappe.db.get_value(
            "Customer",
            {"shopware_customer_id": customer_id},
            "name"
        )

        if customer:
            frappe.logger("shopware6").warning(
                f"Customer {customer_id} deleted in Shopware, linked Customer: {customer}"
            )
            # Don't automatically delete - just log
            create_shopware_log(
                status="Warning",
                method="handle_customer_deleted",
                message=f"Customer {customer_id} deleted in Shopware. Customer {customer} still exists.",
            )

        return True

    # Inventory Handlers

    def handle_stock_changed(
        self,
        product_id: str,
        payload: Dict[str, Any],
        request_id: str = None
    ) -> bool:
        """Handle stock level change."""
        if not self.setting.sync_inventory_from_shopware:
            return True

        from ecommerce_integrations.shopware6.inventory import update_stock_from_shopware

        update_stock_from_shopware(product_id, payload)
        return True


def handle_webhook_event(
    event_type: str,
    entity_id: str,
    payload: Dict[str, Any],
    request_id: str = None
) -> bool:
    """
    Convenience function to handle a webhook event.

    Args:
        event_type: Type of event
        entity_id: Entity ID
        payload: Webhook payload
        request_id: Log entry ID

    Returns:
        True if handled successfully
    """
    handler = WebhookHandler()
    return handler.handle(event_type, entity_id, payload, request_id)
