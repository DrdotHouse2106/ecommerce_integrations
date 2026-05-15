"""
Shopware 6 Order Module

Handles order synchronization between Shopware 6 and ERPNext.
Modular structure following Shopify best practices.
"""

# Main order sync class and functions
# Delivery handling
from ecommerce_integrations.shopware6.order.delivery_handler import (
    create_delivery_note,
)

# Line item handling
from ecommerce_integrations.shopware6.order.line_item_handler import (
    add_order_item,
)

# Order mapping utilities
from ecommerce_integrations.shopware6.order.order_mapper import (
    calculate_delivery_date,
    extract_checkout_field_value,
    get_payment_method_info,
)
from ecommerce_integrations.shopware6.order.order_sync import (
    ShopwareOrder,
    create_sales_order,
    sync_order_by_id,
    sync_order_from_webhook,
    update_order_custom_fields,
    update_order_status,
)

# Payment handling
from ecommerce_integrations.shopware6.order.payment_handler import (
    create_sales_invoice,
    make_payment_entry_against_sales_invoice,
    verify_payment_status_from_shopware,
)

# Scheduled sync functions
from ecommerce_integrations.shopware6.order.scheduled_sync import (
    scheduled_order_sync,
    sync_old_orders,
    sync_orders_from_shopware,
)

# Checkout fields and service items
from ecommerce_integrations.shopware6.order.service_items import (
    process_checkout_fields,
)

# Tax handling
from ecommerce_integrations.shopware6.order.tax_handler import (
    add_order_taxes,
)

__all__ = [
    # Main class
    "ShopwareOrder",
    # Line items
    "add_order_item",
    # Taxes
    "add_order_taxes",
    "calculate_delivery_date",
    # Delivery
    "create_delivery_note",
    # Payment
    "create_sales_invoice",
    "create_sales_order",
    # Mapping
    "extract_checkout_field_value",
    "get_payment_method_info",
    "make_payment_entry_against_sales_invoice",
    # Checkout fields / service items
    "process_checkout_fields",
    "scheduled_order_sync",
    "sync_old_orders",
    # Sync functions
    "sync_order_by_id",
    "sync_order_from_webhook",
    # Scheduled
    "sync_orders_from_shopware",
    "update_order_custom_fields",
    "update_order_status",
    "verify_payment_status_from_shopware",
]
