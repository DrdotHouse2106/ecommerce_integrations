"""
Shopware 6 Order Module

Handles order synchronization between Shopware 6 and ERPNext.
Modular structure following Shopify best practices.
"""

# Main order sync class and functions
from ecommerce_integrations.shopware6.order.order_sync import (
    ShopwareOrder,
    sync_order_by_id,
    create_sales_order,
    sync_order_from_webhook,
    update_order_status,
    update_order_custom_fields,
)

# Order mapping utilities
from ecommerce_integrations.shopware6.order.order_mapper import (
    get_checkout_custom_field,
    get_payment_method_info,
    calculate_delivery_date,
)

# Line item handling
from ecommerce_integrations.shopware6.order.line_item_handler import (
    add_order_item,
)

# Tax handling
from ecommerce_integrations.shopware6.order.tax_handler import (
    add_order_taxes,
)

# Payment handling
from ecommerce_integrations.shopware6.order.payment_handler import (
    create_sales_invoice,
    make_payment_entry_against_sales_invoice,
    verify_payment_status_from_shopware,
)

# Delivery handling
from ecommerce_integrations.shopware6.order.delivery_handler import (
    create_delivery_note,
)

# Service items (Tel. Avis, Forklift)
from ecommerce_integrations.shopware6.order.service_items import (
    add_service_items_if_needed,
    add_service_item_if_needed,
)

# Scheduled sync functions
from ecommerce_integrations.shopware6.order.scheduled_sync import (
    sync_orders_from_shopware,
    scheduled_order_sync,
    sync_old_orders,
)

__all__ = [
    # Main class
    "ShopwareOrder",
    # Sync functions
    "sync_order_by_id",
    "create_sales_order",
    "sync_order_from_webhook",
    "update_order_status",
    "update_order_custom_fields",
    # Mapping
    "get_checkout_custom_field",
    "get_payment_method_info",
    "calculate_delivery_date",
    # Line items
    "add_order_item",
    # Taxes
    "add_order_taxes",
    # Payment
    "create_sales_invoice",
    "make_payment_entry_against_sales_invoice",
    "verify_payment_status_from_shopware",
    # Delivery
    "create_delivery_note",
    # Service items
    "add_service_items_if_needed",
    "add_service_item_if_needed",
    # Scheduled
    "sync_orders_from_shopware",
    "scheduled_order_sync",
    "sync_old_orders",
]
