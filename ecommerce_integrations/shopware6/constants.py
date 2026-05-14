"""Constants for Shopware 6 Integration"""

from frappe import _

# Root item groups to skip during category sync (ERPNext default root nodes)
ROOT_ITEM_GROUPS = {"All Item Groups"}

# Module and DocType names
MODULE_NAME = "shopware6"
SETTING_DOCTYPE = "Shopware Setting"

# DEPRECATED: Using shared Ecommerce Integration Log instead of custom Shopware Log
# This maintains consistency with other integrations (Shopify, Unicommerce)
LOG_DOCTYPE = "Ecommerce Integration Log"  # Changed from "Shopware Log"

# Shopware API version (used for documentation, SDK handles versioning)
API_VERSION = "6.5"

# Webhook events that Shopware can send
WEBHOOK_EVENTS = [
    "order.placed",
    "order.state.changed",
    "order_transaction.paid",
    "order_delivery.written",
    "product.written",
    "customer.written",
]

# Event mapper: Shopware event -> ERPNext handler method
EVENT_MAPPER = {
    "order.placed": "ecommerce_integrations.shopware6.order.sync_order_from_webhook",
    "order.updated": "ecommerce_integrations.shopware6.order.update_order_custom_fields",
    "order.state.changed": "ecommerce_integrations.shopware6.order.update_order_status",
    "order_transaction.state.changed": "ecommerce_integrations.shopware6.payment.handle_transaction_state_change",
    "order_transaction.paid": "ecommerce_integrations.shopware6.invoice.prepare_sales_invoice",
    "order_delivery.written": "ecommerce_integrations.shopware6.fulfillment.prepare_delivery_note",
    "product.written": "ecommerce_integrations.shopware6.product.sync_product_from_webhook",
    "customer.written": "ecommerce_integrations.shopware6.customer.sync_customer_from_webhook",
}

# Field names for custom fields added to ERPNext
CUSTOMER_ID_FIELD = "shopware_customer_id"
ORDER_ID_FIELD = "shopware_order_id"
ORDER_NUMBER_FIELD = "shopware_order_number"
DELIVERY_ID_FIELD = "shopware_delivery_id"
TRANSACTION_ID_FIELD = "shopware_transaction_id"
SUPPLIER_ID_FIELD = "shopware_supplier_id"
ADDRESS_ID_FIELD = "shopware_address_id"
ITEM_SELLING_RATE_FIELD = "standard_rate"

# Shopware Custom Field Set and Field names (in Shopware)
SHOPWARE_CUSTOM_FIELD_SET_NAME = "erpnext_product_fields"
# AI Description Custom Fields (in Shopware)
# These are defaults - configure via Shopware Field Mapping for custom names
SHOPWARE_CUSTOM_FIELD_AI_SHORT_DESCRIPTION = "erpnext_ai_short_description"
SHOPWARE_CUSTOM_FIELD_AI_BENEFITS = "erpnext_ai_benefits"
SHOPWARE_CUSTOM_FIELD_AI_SEO_DESCRIPTION = "erpnext_ai_seo_description"

# Mapping: ERPNext field -> Shopware custom field for AI descriptions
PRODUCT_CUSTOM_FIELDS_MAP = {
    "ai_short_description": SHOPWARE_CUSTOM_FIELD_AI_SHORT_DESCRIPTION,
    "ai_benefits": SHOPWARE_CUSTOM_FIELD_AI_BENEFITS,
    "ai_seo_description": SHOPWARE_CUSTOM_FIELD_AI_SEO_DESCRIPTION,
}

# Shopware Custom Field Set for Categories
SHOPWARE_CATEGORY_CUSTOM_FIELD_SET_NAME = "erpnext_category_fields"

# Category FAQ custom field names (in Shopware)
SHOPWARE_CATEGORY_FAQ1_QUESTION = "erpnext_faq1_question"
SHOPWARE_CATEGORY_FAQ1_ANSWER = "erpnext_faq1_answer"
SHOPWARE_CATEGORY_FAQ2_QUESTION = "erpnext_faq2_question"
SHOPWARE_CATEGORY_FAQ2_ANSWER = "erpnext_faq2_answer"
SHOPWARE_CATEGORY_FAQ3_QUESTION = "erpnext_faq3_question"
SHOPWARE_CATEGORY_FAQ3_ANSWER = "erpnext_faq3_answer"
SHOPWARE_CATEGORY_FAQ4_QUESTION = "erpnext_faq4_question"
SHOPWARE_CATEGORY_FAQ4_ANSWER = "erpnext_faq4_answer"
SHOPWARE_CATEGORY_FAQ5_QUESTION = "erpnext_faq5_question"
SHOPWARE_CATEGORY_FAQ5_ANSWER = "erpnext_faq5_answer"

# Category Priority Custom Field (for frontend sorting)
SHOPWARE_CATEGORY_PRIORITY = "erpnext_priority"

# Mapping: ERPNext Item Group FAQ fields -> Shopware category custom fields
CATEGORY_FAQ_FIELDS_MAP = {
    "faq1_question": SHOPWARE_CATEGORY_FAQ1_QUESTION,
    "faq1_answer": SHOPWARE_CATEGORY_FAQ1_ANSWER,
    "faq2_question": SHOPWARE_CATEGORY_FAQ2_QUESTION,
    "faq2_answer": SHOPWARE_CATEGORY_FAQ2_ANSWER,
    "faq3_question": SHOPWARE_CATEGORY_FAQ3_QUESTION,
    "faq3_answer": SHOPWARE_CATEGORY_FAQ3_ANSWER,
    "faq4_question": SHOPWARE_CATEGORY_FAQ4_QUESTION,
    "faq4_answer": SHOPWARE_CATEGORY_FAQ4_ANSWER,
    "faq5_question": SHOPWARE_CATEGORY_FAQ5_QUESTION,
    "faq5_answer": SHOPWARE_CATEGORY_FAQ5_ANSWER,
}

# Shopware variant attributes (max 3 in Shopware)
SHOPWARE_VARIANTS_ATTR_LIST = ["option1", "option2", "option3"]

# Weight unit mapping: Shopware -> ERPNext
WEIGHT_TO_ERPNEXT_UOM_MAP = {
    "kg": _("Kg"),
    "g": _("Gram"),
    "lb": _("Pound"),
    "oz": _("Ounce"),
}

# Default pagination settings
DEFAULT_PAGE_SIZE = 100

# Background job / lock timeouts (seconds)
STATUS_SYNC_ENQUEUE_TIMEOUT = 60       # seconds — short-running hook handlers
INVOICE_CREATE_ENQUEUE_TIMEOUT = 120   # seconds — invoice/payment creation
ORDER_SYNC_LOCK_TIMEOUT = 120          # seconds — per-order serialization
BULK_SYNC_JOB_TIMEOUT = 1800           # seconds — full queue processing

# Default tax rate (configure in Shopware Settings for your country)
DEFAULT_TAX_RATE = 19.0

# Sync status values
SYNC_STATUS = {
    "PENDING": "Pending",
    "IN_PROGRESS": "In Progress",
    "COMPLETED": "Completed",
    "FAILED": "Failed",
}

# Order state mappings: Shopware state -> ERPNext workflow
ORDER_STATE_MAP = {
    "open": "Draft",
    "in_progress": "To Deliver and Bill",
    "completed": "Completed",
    "cancelled": "Cancelled",
}

# Payment state mappings
# All Shopware payment states mapped to ERPNext status
# States from Shopware order_transaction.state state machine
PAYMENT_STATE_MAP = {
    # Unpaid states
    "open": "Unpaid",
    "in_progress": "Unpaid",       # Payment is being processed
    "pending": "Unpaid",           # Waiting for payment
    "unconfirmed": "Unpaid",       # Payment not confirmed yet
    "reminded": "Unpaid",          # Reminder sent, still unpaid
    "failed": "Failed",            # Payment failed
    # Paid states
    "authorized": "Paid",          # PayPal/Klarna authorization = payment captured
    "paid": "Paid",
    "completed": "Paid",           # Some providers use this instead of "paid"
    # Partial states
    "partially_paid": "Partly Paid",
    "paid_partially": "Partly Paid",  # Shopware uses this spelling
    # Refund states
    "refunded": "Refunded",
    "refunded_partially": "Partly Paid",  # Partial refund = still partly paid
    "chargeback": "Refunded",      # Chargeback = effectively refunded
    # Cancelled
    "cancelled": "Cancelled",
}

# Delivery state mappings
DELIVERY_STATE_MAP = {
    "open": "Not Delivered",
    "shipped": "Shipped",
    "partially_shipped": "Partly Delivered",
    "returned": "Returned",
    "cancelled": "Cancelled",
}

# Payment method mappings: Shopware payment method short name -> ERPNext Mode of Payment
# These mappings support both standard Shopware handlers and SwagPayPal plugin handlers
# The matching logic checks if any key is contained in the shortName (case-insensitive)
# NOTE: Values must match your ERPNext "Mode of Payment" names. Configure them in Shopware Settings.
PAYMENT_METHOD_MAP = {
    # PayPal (SwagPayPal plugin uses various handler names)
    "pay_pal_payment_handler": "PayPal",
    "pay_pal": "PayPal",
    "paypal": "PayPal",
    "swag_paypal": "PayPal",
    # PayPal ACDC (Credit/Debit Card via PayPal)
    "a_c_d_c": "Credit Card",
    "acdc": "Credit Card",
    # PayPal Pay Later
    "pay_later": "PayPal",
    # PayPal PUI (Pay Upon Invoice)
    "p_u_i": "PayPal Invoice",
    "pui": "PayPal Invoice",
    # SEPA
    "s_e_p_a": "SEPA Direct Debit",
    "sepa": "SEPA Direct Debit",
    "debit": "SEPA Direct Debit",
    # Standard Shopware payment methods
    "invoice": "Invoice",
    "pre_payment": "Prepayment",
    "prepayment": "Prepayment",
    "cash": "Cash",
    "cash_payment": "Cash on Delivery",
    # Stripe
    "stripe": "Stripe",
    "stripe_card": "Stripe",
    # Klarna
    "klarna": "Klarna",
    "klarna_pay_later": "Klarna",
    "klarna_pay_now": "Klarna",
    "klarna_slice_it": "Klarna",
    # Credit card
    "card": "Credit Card",
    "creditcard": "Credit Card",
    # Other PayPal APMs (Alternative Payment Methods)
    "bancontact": "PayPal",
    "blik": "PayPal",
    "eps": "PayPal",
    "ideal": "PayPal",
    "multibanco": "PayPal",
    "oxxo": "PayPal",
    "p24": "PayPal",
}

# Default mode of payment when no mapping is found
DEFAULT_MODE_OF_PAYMENT = "Invoice"

# EU member state ISO codes (excluding domestic country)
EU_COUNTRY_CODES = {
    "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR",
    "GR", "HU", "IE", "IT", "LV", "LT", "LU", "MT", "NL", "PL",
    "PT", "RO", "SK", "SI", "ES", "SE",
}

# Domestic country code (configure in Shopware Settings for your country)
DOMESTIC_COUNTRY_CODE = "DE"

# Tax template mapping by delivery scenario
# {company_abbr} will be replaced with actual company abbreviation
# NOTE: Values must match your ERPNext "Sales Taxes and Charges Template" names.
# Configure these in Shopware Settings to match your locale.
TAX_TEMPLATE_MAP = {
    # Domestic delivery - B2B and B2C same
    "domestic": "Domestic Supply - {company_abbr}",
    # EU B2B with valid VAT ID (reverse charge, 0% tax)
    "eu_b2b": "EU B2B Reverse Charge - {company_abbr}",
    # EU B2C without VAT ID (domestic VAT applies)
    "eu_b2c": "EU B2C Supply - {company_abbr}",
    # Third country (non-EU) - export, 0% tax
    "export": "Export to Third Country - {company_abbr}",
}
