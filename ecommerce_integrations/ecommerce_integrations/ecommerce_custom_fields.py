"""Shared custom field definitions for all ecommerce integrations."""

# Unified ecommerce fields on Sales Order — set by all integrations
ECOMMERCE_SALES_ORDER_FIELDS = [
    {
        "fieldname": "ecommerce_section",
        "label": "Ecommerce",
        "fieldtype": "Section Break",
        "collapsible": 1,
        "no_copy": 1,
    },
    {
        "fieldname": "ecommerce_source",
        "label": "Storefront Type",
        "fieldtype": "Data",
        "insert_after": "ecommerce_section",
        "read_only": 1,
        "no_copy": 1,
        "description": "Which ecommerce platform this order came from (e.g., Shopware, Medusa)",
    },
    {
        "fieldname": "ecommerce_sales_channel_id",
        "label": "Sales Channel ID",
        "fieldtype": "Data",
        "insert_after": "ecommerce_source",
        "read_only": 1,
        "hidden": 1,
        "no_copy": 1,
    },
    {
        "fieldname": "ecommerce_sales_channel_name",
        "label": "Sales Channel",
        "fieldtype": "Data",
        "insert_after": "ecommerce_sales_channel_id",
        "read_only": 1,
        "no_copy": 1,
        "description": "Which shop/storefront this order came from",
    },
    {
        "fieldname": "ecommerce_order_confirmed",
        "label": "Order Confirmed",
        "fieldtype": "Check",
        "insert_after": "ecommerce_sales_channel_name",
        "read_only": 1,
        "no_copy": 1,
        "description": "Set automatically when order entry is complete and confirmation email should be sent",
    },
]


# Channel-tracking fields propagated to downstream docs (Sales Invoice, Delivery Note,
# Payment Entry) so the ChannelAwareNotification override + branding/E-Invoice logic
# can resolve the channel for these doctypes too. Same field names as on Sales Order.
ECOMMERCE_DOWNSTREAM_FIELDS = [
    {
        "fieldname": "ecommerce_section",
        "label": "Ecommerce",
        "fieldtype": "Section Break",
        "collapsible": 1,
        "no_copy": 1,
    },
    {
        "fieldname": "ecommerce_source",
        "label": "Storefront Type",
        "fieldtype": "Data",
        "insert_after": "ecommerce_section",
        "read_only": 1,
        "no_copy": 1,
        "description": "Which ecommerce platform this document came from (propagated from the originating Sales Order).",
    },
    {
        "fieldname": "ecommerce_sales_channel_id",
        "label": "Sales Channel ID",
        "fieldtype": "Data",
        "insert_after": "ecommerce_source",
        "read_only": 1,
        "hidden": 1,
        "no_copy": 1,
    },
    {
        "fieldname": "ecommerce_sales_channel_name",
        "label": "Sales Channel",
        "fieldtype": "Data",
        "insert_after": "ecommerce_sales_channel_id",
        "read_only": 1,
        "no_copy": 1,
        "description": "Which shop/storefront this document belongs to. Used to resolve Ecommerce Channel Branding.",
    },
]

# Source type constants
STOREFRONT_SHOPWARE = "Shopware"
STOREFRONT_MEDUSA = "Medusa"

# Canonical ecommerce property names (stored in Item Ecommerce Property table,
# synced via product metadata)
PROP_IS_SURCHARGE = "is_surcharge"

# Checkout field mapping type constants
MAPPING_SALES_ORDER_FIELD = "Sales Order Field"
MAPPING_SERVICE_ITEM = "Service Item"
MAPPING_CUSTOMER_UPDATE = "Customer Update"
MAPPING_INFO_ONLY = "Info Only"
