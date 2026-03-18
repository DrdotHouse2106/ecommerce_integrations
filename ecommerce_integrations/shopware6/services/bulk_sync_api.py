"""Administrative API helpers for Shopware bulk sync."""

import frappe

from ecommerce_integrations.shopware6.services.access import require_shopware_admin


def get_queue_status():
    """Build a read-only snapshot of the bulk-sync queues."""
    from ecommerce_integrations.shopware6.bulk_sync import (
        get_sync_queue,
        is_bulk_mode_active,
        is_processing,
    )

    require_shopware_admin()
    product_queue = get_sync_queue("product")
    properties_queue = get_sync_queue("properties")
    price_queue = get_sync_queue("price")

    return {
        "queue_size": len(product_queue) + len(properties_queue) + len(price_queue),
        "bulk_mode_active": is_bulk_mode_active(),
        "processing": is_processing(),
        "product_queue_size": len(product_queue),
        "properties_queue_size": len(properties_queue),
        "price_queue_size": len(price_queue),
        "product_queue": product_queue[:10],
        "properties_queue": properties_queue[:10],
        "price_queue": price_queue[:10],
    }


def force_process_queue():
    """Trigger immediate bulk queue processing."""
    from ecommerce_integrations.shopware6.bulk_sync import deactivate_bulk_mode

    require_shopware_admin()
    deactivate_bulk_mode()
    frappe.enqueue(
        "ecommerce_integrations.shopware6.bulk_sync.process_bulk_sync_queue",
        queue="long",
        timeout=1800,
        job_name="shopware6_force_bulk_sync",
        now=True,
    )
    return {"status": "Processing started"}


def clear_all_queues():
    """Clear all Shopware bulk-sync queues."""
    from ecommerce_integrations.shopware6.bulk_sync import (
        clear_sync_queue,
        deactivate_bulk_mode,
        release_sync_lock,
    )

    require_shopware_admin()
    clear_sync_queue("product")
    clear_sync_queue("properties")
    clear_sync_queue("price")
    deactivate_bulk_mode()
    release_sync_lock()
    return {"status": "All queues cleared"}
