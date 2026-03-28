MODULE_NAME = "medusa"
SETTING_DOCTYPE = "Medusa Setting"
LOG_DOCTYPE = "Ecommerce Integration Log"

CUSTOMER_ID_FIELD = "medusa_customer_id"
ORDER_ID_FIELD = "medusa_order_id"
PRODUCT_ID_FIELD = "medusa_product_id"
VARIANT_ID_FIELD = "medusa_variant_id"
CATEGORY_ID_FIELD = "medusa_category_id"

API_PRODUCTS = "/admin/products"
API_PRODUCT_VARIANTS = "/admin/products/{product_id}/variants"
API_ORDERS = "/admin/orders"
API_CUSTOMERS = "/admin/customers"
API_STOCK_LOCATIONS = "/admin/stock-locations"
API_INVENTORY_ITEMS = "/admin/inventory-items"
API_INVENTORY_LEVELS = "/admin/inventory-items/{id}/location-levels"
API_INVENTORY_LEVELS_BATCH = "/admin/inventory-items/location-levels/batch"
API_CATEGORIES = "/admin/product-categories"
API_COLLECTIONS = "/admin/collections"
API_FULFILLMENTS = "/admin/orders/{order_id}/fulfillments"
API_AUTH = "/auth/user/emailpass"

ORDER_STATUS_MAP = {
    "pending": "Draft",
    "completed": "Completed",
    "archived": "Closed",
    "canceled": "Cancelled",
    "requires_action": "On Hold",
}

PAYMENT_STATUS_MAP = {
    "not_paid": "Unpaid",
    "awaiting": "Unpaid",
    "authorized": "Unpaid",
    "partially_authorized": "Partly Paid",
    "captured": "Paid",
    "partially_captured": "Partly Paid",
    "partially_refunded": "Partly Paid",
    "refunded": "Refunded",
    "canceled": "Cancelled",
    "requires_action": "Unpaid",
}

FULFILLMENT_STATUS_MAP = {
    "not_fulfilled": "Not Delivered",
    "partially_fulfilled": "Partly Delivered",
    "fulfilled": "Fully Delivered",
    "partially_shipped": "Partly Delivered",
    "shipped": "Shipped",
    "partially_delivered": "Partly Delivered",
    "delivered": "Delivered",
    "canceled": "Cancelled",
    "partially_returned": "Return Issued",
    "returned": "Return Issued",
}

WEBHOOK_EVENTS = [
    "order.placed",
    "order.updated",
    "order.canceled",
    "order.completed",
    "order.fulfillment_created",
    "order.fulfillment_canceled",
    "customer.created",
    "customer.updated",
    "product.created",
    "product.updated",
    "product.deleted",
]

MEDUSA_PRICE_FACTOR = 100
