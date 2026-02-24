"""
Hooks for Shopware 6 Integration

This file is not directly used - hooks are defined in the main app hooks.py.
This is a reference for what needs to be added to the main hooks.
"""

# Add these to the main ecommerce_integrations/hooks.py:

# Scheduler events for inventory sync
SCHEDULER_EVENTS = {
    "cron": {
        # Run every 5 minutes
        "*/5 * * * *": [
            "ecommerce_integrations.shopware6.inventory.sync_inventory_to_shopware"
        ],
    }
}

# Document events for stock sync, property sync, and status sync
DOC_EVENTS = {
    "Stock Entry": {
        "on_submit": "ecommerce_integrations.shopware6.inventory.update_stock_on_stock_entry",
    },
    "Item": {
        "after_save": "ecommerce_integrations.shopware6.properties.upload_item_properties",
    },
    "Item Group": {
        "on_update": "ecommerce_integrations.shopware6.bulk_sync.queue_item_group_for_sync",
        "after_rename": "ecommerce_integrations.shopware6.bulk_sync.queue_item_group_rename_for_sync",
        "on_trash": "ecommerce_integrations.shopware6.bulk_sync.queue_item_group_delete_for_sync",
    },
    # Status sync: ERPNext -> Shopware
    "Delivery Note": {
        "on_submit": "ecommerce_integrations.shopware6.status_sync.on_delivery_note_submit",
        "on_cancel": "ecommerce_integrations.shopware6.status_sync.on_delivery_note_cancel",
    },
    "Sales Invoice": {
        "on_submit": "ecommerce_integrations.shopware6.status_sync.on_sales_invoice_submit",
    },
    "Sales Order": {
        "on_cancel": "ecommerce_integrations.shopware6.status_sync.on_sales_order_cancel",
    },
    "Payment Entry": {
        "on_submit": "ecommerce_integrations.shopware6.status_sync.on_payment_entry_submit",
    },
}

# Fixtures for custom fields
FIXTURES = [
    {
        "dt": "Custom Field",
        "filters": [
            ["name", "in", [
                # Customer fields
                "Customer-shopware_customer_id",
                # Sales Order fields
                "Sales Order-shopware_section",
                "Sales Order-shopware_order_id",
                "Sales Order-shopware_order_number",
                "Sales Order-shopware_payment_method",
                "Sales Order-shopware_payment_status",
                "Sales Order-shopware_erpnext_mode_of_payment",
                # Delivery Note fields
                "Delivery Note-shopware_delivery_id",
                # Sales Invoice fields
                "Sales Invoice-shopware_transaction_id",
            ]]
        ]
    }
]
