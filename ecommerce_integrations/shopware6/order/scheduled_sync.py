"""
Shopware 6 Scheduled Sync

Scheduled jobs for order synchronization.
"""

from typing import Dict

import frappe
from frappe import _
from frappe.utils import nowdate, add_days

from lib_shopware6_api_base import Criteria, RangeFilter

from ecommerce_integrations.shopware6.connection import get_shopware_client
from ecommerce_integrations.shopware6.constants import SETTING_DOCTYPE
from ecommerce_integrations.shopware6.utils import get_logger

SCHEDULED_SYNC_LOCK_KEY = "shopware6_scheduled_order_sync"
SCHEDULED_SYNC_LOCK_TIMEOUT_SECONDS = 15 * 60


def build_order_criteria(
    from_date: str = None,
    to_date: str = None,
    limit: int = 100
) -> Criteria:
    """
    Build a Criteria object for order search with associations.

    Args:
        from_date: Start date for order filter (YYYY-MM-DD)
        to_date: End date for order filter (YYYY-MM-DD)
        limit: Maximum number of orders

    Returns:
        Criteria object with all associations
    """
    criteria = Criteria(limit=int(limit))

    # Add date filters if provided
    if from_date or to_date:
        date_filter = {}
        if from_date:
            date_filter["gte"] = f"{from_date}T00:00:00.000Z"
        if to_date:
            date_filter["lte"] = f"{to_date}T23:59:59.999Z"
        criteria.filter.append(RangeFilter(field="orderDateTime", parameters=date_filter))

    # Add associations
    # stateMachineState needed to check order state (skip cancelled orders)
    criteria.associations["stateMachineState"] = Criteria()
    criteria.associations["lineItems"] = Criteria()
    criteria.associations["orderCustomer"] = Criteria()
    criteria.associations["orderCustomer"].associations["salutation"] = Criteria()
    criteria.associations["orderCustomer"].associations["customer"] = Criteria()  # For accountType, vatIds
    criteria.associations["billingAddress"] = Criteria()
    criteria.associations["billingAddress"].associations["country"] = Criteria()
    criteria.associations["billingAddress"].associations["countryState"] = Criteria()
    criteria.associations["billingAddress"].associations["salutation"] = Criteria()
    criteria.associations["deliveries"] = Criteria()
    criteria.associations["deliveries"].associations["shippingOrderAddress"] = Criteria()
    criteria.associations["deliveries"].associations["shippingOrderAddress"].associations["country"] = Criteria()
    criteria.associations["deliveries"].associations["shippingOrderAddress"].associations["countryState"] = Criteria()
    criteria.associations["deliveries"].associations["shippingOrderAddress"].associations["salutation"] = Criteria()
    criteria.associations["deliveries"].associations["shippingMethod"] = Criteria()
    criteria.associations["transactions"] = Criteria()
    criteria.associations["transactions"].associations["paymentMethod"] = Criteria()
    criteria.associations["transactions"].associations["stateMachineState"] = Criteria()
    criteria.associations["currency"] = Criteria()
    # Multi-Storefront: Fetch sales channel info
    criteria.associations["salesChannel"] = Criteria()

    return criteria


@frappe.whitelist()
def sync_orders_from_shopware(
    from_date: str = None,
    to_date: str = None,
    limit: int = 100
) -> Dict[str, int]:
    """
    Manually sync orders from Shopware.

    Args:
        from_date: Start date for order filter (YYYY-MM-DD)
        to_date: End date for order filter (YYYY-MM-DD)
        limit: Maximum number of orders to sync

    Returns:
        dict: Sync results
    """
    from ecommerce_integrations.shopware6.order.order_sync import create_sales_order

    client = get_shopware_client()
    criteria = build_order_criteria(from_date, to_date, limit)

    response = client.request_post("search/order", criteria)
    orders = response.get("data", [])

    synced = 0
    errors = 0

    for order_data in orders:
        try:
            order_id = order_data.get("id")

            # Skip cancelled orders (safety net - API filter should already exclude them)
            order_state = (order_data.get("stateMachineState") or {}).get("technicalName", "")
            if order_state == "cancelled":
                continue

            # Check if already synced
            existing = frappe.db.get_value("Sales Order", {"shopware_order_id": order_id}, "name")
            if not existing:
                create_sales_order(order_data)
                synced += 1
        except Exception as e:
            errors += 1
            # CRITICAL: Rollback to release any locks held by the failed transaction.
            # Without this, stuck locks block all subsequent sync attempts.
            frappe.db.rollback()
            get_logger().error(f"Failed to sync order {order_data.get('orderNumber')}: {e}", persist=True)

    return {
        "synced": synced,
        "errors": errors,
        "total": len(orders),
    }


def scheduled_order_sync():
    """
    Scheduled job to sync orders from Shopware.
    Respects the order_sync_frequency setting.
    Called every minute by scheduler, but only runs if enough time has passed.
    """
    from frappe.utils import now_datetime, time_diff_in_seconds

    logger = get_logger("scheduled_order_sync")
    setting = frappe.get_doc(SETTING_DOCTYPE)

    if not setting.is_enabled():
        return

    # Get frequency in minutes (default 60)
    frequency = int(setting.order_sync_frequency or 60)
    frequency_seconds = frequency * 60

    # Check if enough time has passed since last sync
    if setting.last_order_sync:
        elapsed = time_diff_in_seconds(now_datetime(), setting.last_order_sync)
        if elapsed < frequency_seconds:
            return  # Not time yet

    # Prevent overlapping scheduler runs. Overlap can hold open transactions
    # on the same customer and cascade into lock wait timeouts.
    lock = frappe.cache().lock(
        SCHEDULED_SYNC_LOCK_KEY,
        timeout=SCHEDULED_SYNC_LOCK_TIMEOUT_SECONDS,
    )
    if not lock.acquire(blocking=False):
        logger.info("Skipping run because a previous scheduled order sync is still in progress")
        return

    try:
        # Sync orders from the last 24 hours
        result = sync_orders_from_shopware(
            from_date=add_days(nowdate(), -1),
            limit=100
        )

        # Update last sync time
        frappe.db.set_single_value(SETTING_DOCTYPE, "last_order_sync", now_datetime())
        frappe.db.commit()

        if result.get("synced", 0) > 0:
            frappe.logger("shopware6").info(
                f"Order sync completed: {result['synced']} synced, {result['errors']} errors"
            )

    except Exception as e:
        frappe.db.rollback()
        logger.error(f"Scheduled order sync failed: {e}", persist=True)
    finally:
        try:
            lock.release()
        except Exception:
            # Ignore release errors; lock timeout acts as safety net.
            pass


def sync_old_orders():
    """
    Scheduled job to sync old orders from Shopware.
    Similar to Shopify's sync_old_orders functionality.
    This allows syncing orders that existed before the integration was set up.
    Called hourly by scheduler.
    """
    from frappe.utils import cint, get_datetime
    from ecommerce_integrations.shopware6.order.order_sync import create_sales_order

    setting = frappe.get_cached_doc(SETTING_DOCTYPE)

    if not cint(setting.sync_old_orders):
        return

    if not setting.is_enabled():
        return

    if not setting.old_orders_from or not setting.old_orders_to:
        logger = get_logger("scheduled_old_order_sync")
        logger.warning(
            "old_orders_from and old_orders_to are required for syncing old orders",
            persist=True
        )
        return

    try:
        # Convert to ISO format for API
        from_time = get_datetime(setting.old_orders_from).astimezone().isoformat()
        to_time = get_datetime(setting.old_orders_to).astimezone().isoformat()

        # Get client directly - don't use decorator on scheduled jobs
        client = get_shopware_client()

        # Use pagination to fetch all orders in the date range
        criteria = Criteria(limit=100)

        # Add date filter
        criteria.filter.append(RangeFilter(field="orderDateTime", parameters={"gte": from_time, "lte": to_time}))

        # Exclude cancelled orders
        # Add associations
        # stateMachineState needed to check order state (skip cancelled orders)
        criteria.associations["stateMachineState"] = Criteria()
        criteria.associations["lineItems"] = Criteria()
        criteria.associations["orderCustomer"] = Criteria()
        criteria.associations["orderCustomer"].associations["salutation"] = Criteria()
        criteria.associations["billingAddress"] = Criteria()
        criteria.associations["billingAddress"].associations["country"] = Criteria()
        criteria.associations["billingAddress"].associations["countryState"] = Criteria()
        criteria.associations["deliveries"] = Criteria()
        criteria.associations["deliveries"].associations["shippingOrderAddress"] = Criteria()
        criteria.associations["deliveries"].associations["shippingOrderAddress"].associations["country"] = Criteria()
        criteria.associations["deliveries"].associations["shippingOrderAddress"].associations["countryState"] = Criteria()
        criteria.associations["deliveries"].associations["shippingMethod"] = Criteria()
        criteria.associations["transactions"] = Criteria()
        criteria.associations["transactions"].associations["paymentMethod"] = Criteria()
        criteria.associations["transactions"].associations["stateMachineState"] = Criteria()
        criteria.associations["currency"] = Criteria()

        response = client.request_post("search/order", criteria)
        orders = response.get("data", [])

        synced = 0
        skipped = 0
        errors = 0

        for order_data in orders:
            try:
                order_id = order_data.get("id")

                # Skip cancelled orders
                order_state = (order_data.get("stateMachineState") or {}).get("technicalName", "")
                if order_state == "cancelled":
                    skipped += 1
                    continue

                # Check if already synced
                existing = frappe.db.get_value("Sales Order", {"shopware_order_id": order_id}, "name")
                if existing:
                    skipped += 1
                    continue

                # Create order using the standard function
                create_sales_order(order_data)
                synced += 1

            except Exception as e:
                errors += 1
                frappe.db.rollback()
                logger = get_logger("scheduled_old_order_sync")
                logger.error(
                    f"Failed to sync old order {order_data.get('orderNumber')}",
                    exception=e,
                    persist=True
                )

        # Disable the flag after successful sync (consistent with Shopify implementation)
        if errors == 0:
            setting = frappe.get_doc(SETTING_DOCTYPE)
            setting.sync_old_orders = 0
            setting.save()

        frappe.logger("shopware6").info(
            f"Old order sync completed: {synced} synced, {skipped} skipped, {errors} errors"
        )

    except Exception as e:
        get_logger().error(f"Scheduled old order sync failed: {e}", persist=True)
