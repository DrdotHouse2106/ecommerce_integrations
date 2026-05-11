"""
Shopware 6 Inventory Sync Module

Handles synchronization of stock levels between ERPNext and Shopware 6.
"""

from typing import Any

import frappe
from frappe import _
from frappe.utils import flt, now_datetime

from lib_shopware6_api_base import (
    Shopware6AdminAPIClientBase,
    HEADER_index_asynchronously,
)

from ecommerce_integrations.shopware6.connection import get_shopware_client
from ecommerce_integrations.shopware6.constants import (
    MODULE_NAME,
    SETTING_DOCTYPE,
)
from ecommerce_integrations.shopware6.utils import get_logger


def sync_inventory_to_shopware():
    """
    Scheduled job to sync ERPNext stock levels to Shopware.

    This function is called by the scheduler based on the configured frequency.
    """
    logger = get_logger("sync_inventory_to_shopware")
    setting = frappe.get_doc(SETTING_DOCTYPE)

    if not setting.is_enabled() or not setting.update_erpnext_stock_levels_to_shopware:
        return

    try:
        result = sync_all_inventory()
        logger.info(
            f"Inventory sync completed: {result['synced']} synced, {result['errors']} errors",
            persist=True
        )

        # Update last sync time
        setting.last_inventory_sync = now_datetime()
        setting.save(ignore_permissions=True)
        frappe.db.commit()

    except Exception as e:
        logger.error(
            "Shopware inventory sync failed",
            exception=e,
            persist=True
        )


def sync_all_inventory() -> dict[str, int]:
    """
    Sync all synced items' stock levels to Shopware.

    Uses batch updates for efficiency with per-batch commits for resilience.
    Following Shopify best practice of granular error handling.

    Returns:
        dict: Sync results with success/error counts
    """
    logger = get_logger("sync_all_inventory")
    setting = frappe.get_doc(SETTING_DOCTYPE)
    client = get_shopware_client()

    # Get all synced items
    ecommerce_items = frappe.get_all(
        "Ecommerce Item",
        filters={"integration": MODULE_NAME, "has_variants": 0},
        fields=["name", "erpnext_item_code", "integration_item_code", "variant_id"],
    )

    logger.info(f"Starting inventory sync for {len(ecommerce_items)} items")

    synced = 0
    errors = 0
    batch_size = 50

    # Batch stock updates for efficiency
    stock_updates = []

    # Pre-fetch all stock quantities in one query to avoid N+1
    item_codes = [e.erpnext_item_code for e in ecommerce_items]
    stock_map = _batch_get_stock_qty(item_codes, setting.warehouse)

    for ecom_item in ecommerce_items:
        try:
            stock_qty = stock_map.get(ecom_item.erpnext_item_code, 0)

            # Prepare update payload
            product_id = ecom_item.integration_item_code

            stock_updates.append({
                "id": product_id,
                "stock": int(stock_qty),
                "item_code": ecom_item.erpnext_item_code,  # For error tracking
            })

            # Send in batches
            if len(stock_updates) >= batch_size:
                batch_result = _send_stock_updates_with_tracking(client, stock_updates)
                synced += batch_result["success"]
                errors += batch_result["errors"]
                stock_updates = []
                # Commit after each batch for resilience (Shopify pattern)
                frappe.db.commit()

        except Exception as e:
            errors += 1
            logger.error(
                f"Failed to prepare stock for {ecom_item.erpnext_item_code}",
                exception=e,
                persist=False
            )

    # Send remaining updates
    if stock_updates:
        batch_result = _send_stock_updates_with_tracking(client, stock_updates)
        synced += batch_result["success"]
        errors += batch_result["errors"]
        frappe.db.commit()

    logger.info(f"Inventory sync completed: {synced} synced, {errors} errors")
    return {"synced": synced, "errors": errors}


def _send_stock_updates_with_tracking(client: Shopware6AdminAPIClientBase, updates: list[dict]) -> dict[str, int]:
    """
    Send batch stock updates with granular error tracking (Shopify pattern).

    First tries batch update, then falls back to individual updates on failure,
    tracking success/failure for each item.

    Returns:
        dict: {"success": count, "errors": count}
    """
    logger = get_logger("_send_stock_updates_with_tracking")
    success = 0
    errors = 0

    # Try batch update first
    try:
        _send_stock_updates(client, updates)
        success = len(updates)
        logger.debug(f"Batch stock update successful for {success} items")
    except Exception as batch_error:
        # Fall back to individual updates with tracking
        logger.warning(
            f"Batch stock update failed, falling back to individual: {batch_error}",
            persist=False
        )
        for update in updates:
            try:
                client.request_patch(f"product/{update['id']}", {"stock": update["stock"]})
                success += 1
            except Exception as e:
                errors += 1
                item_code = update.get("item_code", update["id"])
                logger.error(
                    f"Failed to update stock for {item_code}",
                    exception=e,
                    persist=False
                )

    return {"success": success, "errors": errors}


def _send_stock_updates(client: Shopware6AdminAPIClientBase, updates: list[dict]) -> None:
    """
    Send batch stock updates to Shopware using the sync API.

    Shopware 6 uses the _action/sync endpoint for bulk updates.
    """
    # Prepare sync payload
    sync_payload = {
        "write-product": {
            "entity": "product",
            "action": "upsert",
            "payload": []
        }
    }

    for update in updates:
        sync_payload["write-product"]["payload"].append({
            "id": update["id"],
            "stock": update["stock"],
        })

    # Use async indexing for better performance (reduces response time from 30-60s to 1-2s)
    client.request_post("_action/sync", sync_payload, update_header_fields=HEADER_index_asynchronously)


def _batch_get_stock_qty(item_codes: list, warehouse: str = None) -> dict:
    """Fetch stock quantities for multiple items in a single query.

    Returns:
        Dict mapping item_code to stock quantity.
    """
    if not item_codes:
        return {}

    if warehouse:
        rows = frappe.db.sql(
            """
            SELECT item_code, SUM(actual_qty) as qty
            FROM `tabBin`
            WHERE item_code IN ({}) AND warehouse = %s
            GROUP BY item_code
            """.format(", ".join(["%s"] * len(item_codes))),
            (*item_codes, warehouse),
            as_dict=True,
        )
    else:
        rows = frappe.db.sql(
            """
            SELECT item_code, SUM(actual_qty) as qty
            FROM `tabBin`
            WHERE item_code IN ({})
            GROUP BY item_code
            """.format(", ".join(["%s"] * len(item_codes))),
            item_codes,
            as_dict=True,
        )

    return {row.item_code: flt(row.qty) for row in rows}


def get_stock_qty(item_code: str, warehouse: str = None) -> float:
    """
    Get actual stock quantity for an item.

    Args:
        item_code: ERPNext Item code
        warehouse: Optional specific warehouse

    Returns:
        Stock quantity as float
    """
    from erpnext.stock.utils import get_stock_balance

    if warehouse:
        return flt(get_stock_balance(item_code, warehouse))

    # Get total stock across all warehouses
    return flt(frappe.db.sql(
        """
        SELECT SUM(actual_qty)
        FROM `tabBin`
        WHERE item_code = %s
        """,
        item_code
    )[0][0])


@frappe.whitelist()
def sync_single_item_stock(item_code: str) -> dict[str, Any]:
    """
    Sync stock for a single item to Shopware.

    Args:
        item_code: ERPNext Item code

    Returns:
        dict: Result of sync operation
    """
    frappe.only_for("System Manager")
    setting = frappe.get_doc(SETTING_DOCTYPE)

    if not setting.is_enabled():
        return {"success": False, "message": _("Shopware integration is not enabled")}

    # Find the ecommerce item
    ecom_item = frappe.db.get_value(
        "Ecommerce Item",
        {"integration": MODULE_NAME, "erpnext_item_code": item_code},
        ["integration_item_code", "variant_id"],
        as_dict=True
    )

    if not ecom_item:
        return {"success": False, "message": _("Item is not synced with Shopware")}

    try:
        client = get_shopware_client()
        stock_qty = get_stock_qty(item_code, setting.warehouse)

        client.request_patch(
            f"product/{ecom_item.integration_item_code}",
            {"stock": int(stock_qty)}
        )

        return {
            "success": True,
            "message": _("Stock updated successfully"),
            "stock": stock_qty,
        }

    except Exception as e:
        return {"success": False, "message": str(e)}


def update_stock_on_stock_entry(doc, method):
    """
    Hook to sync stock after Stock Entry submission.

    Add this to hooks.py:
    doc_events = {
        "Stock Entry": {
            "on_submit": "ecommerce_integrations.shopware6.inventory.update_stock_on_stock_entry"
        }
    }
    """
    setting = frappe.get_doc(SETTING_DOCTYPE)

    if not setting.is_enabled() or not setting.update_erpnext_stock_levels_to_shopware:
        return

    # Get unique items from the stock entry
    items = set()
    for item in doc.items:
        items.add(item.item_code)

    # Queue stock sync for affected items
    for item_code in items:
        frappe.enqueue(
            "ecommerce_integrations.shopware6.inventory.sync_single_item_stock",
            queue="short",
            item_code=item_code,
        )


def update_stock_on_stock_reconciliation(doc, method):
    """
    Hook to sync stock after Stock Reconciliation submission.

    When a Stock Reconciliation is submitted, the affected items
    should have their stock levels synced to Shopware.
    """
    setting = frappe.get_doc(SETTING_DOCTYPE)

    if not setting.is_enabled() or not setting.update_erpnext_stock_levels_to_shopware:
        return

    # Get unique items from the stock reconciliation
    items = set()
    for item in doc.items:
        if item.item_code:
            items.add(item.item_code)

    # Queue stock sync for affected items
    for item_code in items:
        frappe.enqueue(
            "ecommerce_integrations.shopware6.inventory.sync_single_item_stock",
            queue="short",
            item_code=item_code,
        )
