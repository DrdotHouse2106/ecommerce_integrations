MODULE_NAME = "medusa"
SETTING_DOCTYPE = "Medusa Setting"
LOG_DOCTYPE = "Ecommerce Integration Log"

CUSTOMER_ID_FIELD = "medusa_customer_id"
ORDER_ID_FIELD = "medusa_order_id"
CATEGORY_ID_FIELD = "medusa_category_id"

# PRODUCT_ID_FIELD / VARIANT_ID_FIELD were the legacy ``tabItem``
# custom-field names; they are no longer used. Medusa product / variant
# ids now live in ``tabEcommerce Item`` (integration='medusa') — see
# ``medusa/utils.py`` for the canonical accessors and
# ``patches/migrate_medusa_ids_to_ecommerce_item.py`` for the migration.

API_PRODUCTS = "/admin/products"
API_PRODUCTS_BATCH = "/admin/products/batch"
API_PRODUCT_VARIANTS = "/admin/products/{product_id}/variants"
API_PRODUCT_VARIANTS_BATCH = "/admin/products/{product_id}/variants/batch"
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
API_FEATURE_FLAGS = "/admin/feature-flags"
API_INDEX_SYNC = "/admin/index/sync"
API_INDEX_DETAILS = "/admin/index/details"

# Values for ``Medusa Setting.category_assignment_mode``. The default
# mirrors Item Groups to Medusa categories; the alternative leaves
# category assignment to the Smart Collections engine.
CATEGORY_MODE_ITEM_GROUP_MAPPING = "Item Group Mapping"
CATEGORY_MODE_SMART_COLLECTIONS_ONLY = "Smart Collections Only"

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

# Medusa v2 stores prices as decimal values (49.99 = EUR 49.99), not cents.
# Set to 1 for v2. Legacy v1 used 100 (cents).
MEDUSA_PRICE_FACTOR = 1
