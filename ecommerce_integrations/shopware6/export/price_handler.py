"""
Shopware 6 Price Handler

Handles price synchronization from ERPNext to Shopware.
Supports multiple price lists and currency handling.
"""

from typing import Any, Dict, List, Optional

import frappe
from frappe.utils import flt

from ecommerce_integrations.shopware6.connection import temp_shopware_session
from ecommerce_integrations.shopware6.constants import ITEM_SELLING_RATE_FIELD
from ecommerce_integrations.shopware6.export.utils import get_shopware_document_id
from ecommerce_integrations.shopware6.export.product_mapper import get_cached_currency_id


def get_item_price(item_code: str, price_list: str = None) -> float:
    """
    Get the selling price for an item.

    Args:
        item_code: ERPNext Item code
        price_list: Price list to use (optional). If specified, only looks in that price list.

    Returns:
        Price as float
    """
    # If price_list is specified, prioritize Item Price table
    if price_list:
        item_price = frappe.db.get_value(
            "Item Price",
            {
                "item_code": item_code,
                "price_list": price_list,
                "selling": 1,
                "price_list_rate": [">", 0]
            },
            "price_list_rate"
        )
        if item_price:
            return flt(item_price)

    # Try standard_rate as fallback
    item = frappe.get_doc("Item", item_code)
    price = flt(item.get(ITEM_SELLING_RATE_FIELD) or 0)

    if price > 0:
        return price

    # Try Item Price table (any price list)
    filters = {"item_code": item_code, "selling": 1, "price_list_rate": [">", 0]}

    item_price = frappe.db.get_value(
        "Item Price",
        filters,
        "price_list_rate",
        order_by="price_list_rate desc"
    )

    return flt(item_price) if item_price else 0.0


def get_channel_price(item_code: str, sales_channel, setting) -> float:
    """
    Get the price for an item in a specific sales channel.

    Priority:
    1. If channel has price_list set -> use that price list
    2. If channel has price_adjustment_percent -> apply to base price
    3. Fallback -> use setting.default_selling_price_list

    Args:
        item_code: ERPNext Item code
        sales_channel: Shopware Sales Channel row from setting
        setting: Shopware Setting document

    Returns:
        Price as float
    """
    # Priority 1: Channel has its own price list
    if sales_channel.price_list:
        return get_item_price(item_code, sales_channel.price_list)

    # Get base price from default price list
    default_price_list = getattr(setting, 'default_selling_price_list', None)
    base_price = get_item_price(item_code, default_price_list)

    # Priority 2: Channel has percentage adjustment
    adjustment_percent = flt(getattr(sales_channel, 'price_adjustment_percent', 0))
    if adjustment_percent != 0:
        adjusted_price = base_price * (1 + adjustment_percent / 100)
        return round(adjusted_price, 2)

    # Priority 3: Return base price as-is
    return base_price


def get_item_tax_rate(item_code: str) -> float:
    """
    Get the tax rate for an item from its tax template.

    Args:
        item_code: ERPNext Item code

    Returns:
        Tax rate as float (default 19.0)
    """
    item = frappe.get_doc("Item", item_code)
    tax_rate = 19.0  # Default German VAT

    if hasattr(item, 'taxes') and item.taxes:
        for tax_row in item.taxes:
            if tax_row.item_tax_template:
                template_rate = frappe.db.get_value(
                    "Item Tax Template Detail",
                    {"parent": tax_row.item_tax_template},
                    "tax_rate"
                )
                if template_rate:
                    tax_rate = flt(template_rate)
                    break

    return tax_rate


def build_price_payload(
    net_price: float,
    tax_rate: float = 19.0,
    currency_id: str = None
) -> List[Dict[str, Any]]:
    """
    Build the Shopware price payload.

    Args:
        net_price: Net price (without tax)
        tax_rate: Tax rate percentage
        currency_id: Shopware currency ID

    Returns:
        Price array for Shopware product

    Raises:
        ValueError: If net_price is <= 0
    """
    # CRITICAL: Never use 0.01 fallback - this causes pricing errors in Shopware
    if net_price <= 0:
        raise ValueError(
            f"Cannot build price payload with net_price <= 0 ({net_price}). "
            "Ensure a valid price exists in ERPNext before syncing to Shopware."
        )

    gross_price = round(net_price * (1 + tax_rate / 100), 2)

    return [{
        "currencyId": currency_id,
        "gross": gross_price,
        "net": net_price,
        "linked": False,
    }]


@temp_shopware_session
def sync_product_price(client, item_code: str) -> bool:
    """
    Sync the price of an ERPNext Item to Shopware.

    Args:
        client: Shopware API client
        item_code: ERPNext Item code

    Returns:
        True if successful
    """
    shopware_id = get_shopware_document_id("Item", item_code)
    if not shopware_id:
        frappe.log_error(f"Cannot sync price for {item_code}: not synced to Shopware")
        return False

    try:
        # Get price and tax rate
        price = get_item_price(item_code)
        tax_rate = get_item_tax_rate(item_code)
        currency_id = get_cached_currency_id(client, "EUR")

        # Build payload
        price_payload = build_price_payload(price, tax_rate, currency_id)

        # Update product
        client.request_patch(f"product/{shopware_id}", {"price": price_payload})

        frappe.logger().info(f"Synced price for {item_code}: {price} EUR (net)")
        return True

    except Exception as e:
        frappe.log_error(f"Failed to sync price for {item_code}: {e}")
        return False


@temp_shopware_session
def sync_bulk_prices(client, item_codes: List[str]) -> Dict[str, bool]:
    """
    Sync prices for multiple items efficiently.

    Args:
        client: Shopware API client
        item_codes: List of ERPNext Item codes

    Returns:
        Dict mapping item_code to success status
    """
    results = {}
    currency_id = get_cached_currency_id(client, "EUR")

    for item_code in item_codes:
        shopware_id = get_shopware_document_id("Item", item_code)
        if not shopware_id:
            results[item_code] = False
            continue

        try:
            price = get_item_price(item_code)
            tax_rate = get_item_tax_rate(item_code)
            price_payload = build_price_payload(price, tax_rate, currency_id)

            client.request_patch(f"product/{shopware_id}", {"price": price_payload})
            results[item_code] = True

        except Exception as e:
            frappe.log_error(f"Failed to sync price for {item_code}: {e}")
            results[item_code] = False

    success_count = sum(1 for v in results.values() if v)
    frappe.logger().info(f"Bulk price sync: {success_count}/{len(item_codes)} successful")

    return results


def update_item_price_in_shopware(item_code: str = None, doc=None) -> Dict[str, Any]:
    """
    Update price for a single item in Shopware.

    Convenience function that returns a dict with success status and message.
    Can be called with either item_code or doc.

    Args:
        item_code: ERPNext Item code (if doc not provided)
        doc: ERPNext Item document (optional)

    Returns:
        Dict with 'success' bool and 'message' string
    """
    if doc:
        item_code = doc.name

    if not item_code:
        return {"success": False, "message": "No item_code provided"}

    try:
        result = sync_product_price(item_code)
        if result:
            return {"success": True, "message": f"Updated price for {item_code}"}
        else:
            return {"success": False, "message": f"Failed to update price for {item_code}"}
    except Exception as e:
        return {"success": False, "message": str(e)}


# =============================================================================
# FORCED PRICE RECONCILIATION
# =============================================================================

@temp_shopware_session
def force_sync_single_product_price(
    client,
    item_code: str,
    price_list: str = None
) -> Dict[str, Any]:
    """
    Force sync price for a single product - deletes all existing prices first.

    This function:
    1. Gets the Shopware product ID
    2. Fetches existing prices from Shopware
    3. Deletes all existing product_price entries
    4. Creates new prices based on ERPNext data

    Args:
        client: Shopware API client
        item_code: ERPNext Item code
        price_list: Optional price list to use (defaults to Standard-Vertrieb)

    Returns:
        Dict with success status and details
    """
    shopware_id = get_shopware_document_id("Item", item_code)
    if not shopware_id:
        return {
            "success": False,
            "message": f"Item {item_code} not synced to Shopware"
        }

    try:
        # Get current prices from Shopware
        response = client.request_post("search/product-price", {
            "filter": [
                {"type": "equals", "field": "productId", "value": shopware_id}
            ],
            "limit": 100
        })
        existing_prices = response.get("data", [])

        # Delete all existing prices
        deleted_count = 0
        for price_entry in existing_prices:
            price_id = price_entry.get("id")
            if price_id:
                try:
                    client.request_delete(f"product-price/{price_id}")
                    deleted_count += 1
                except Exception as e:
                    frappe.logger().warning(f"Failed to delete price {price_id}: {e}")

        # Get new price from ERPNext
        if not price_list:
            price_list = "Standard-Vertrieb"

        net_price = get_item_price(item_code, price_list)
        if net_price <= 0:
            # Fallback to any price
            net_price = get_item_price(item_code)

        tax_rate = get_item_tax_rate(item_code)
        currency_id = get_cached_currency_id(client, "EUR")

        # CRITICAL: Validate price before syncing - never use 0.01 fallback
        if net_price <= 0:
            return {
                "success": False,
                "item_code": item_code,
                "shopware_id": shopware_id,
                "message": f"No valid price found for {item_code}. Cannot sync to Shopware with price <= 0."
            }

        # Calculate gross price
        gross_price = round(net_price * (1 + tax_rate / 100), 2)

        # Update product with new base price
        price_payload = [{
            "currencyId": currency_id,
            "gross": gross_price,
            "net": net_price,
            "linked": False,
        }]

        client.request_patch(f"product/{shopware_id}", {"price": price_payload})

        return {
            "success": True,
            "item_code": item_code,
            "shopware_id": shopware_id,
            "deleted_prices": deleted_count,
            "new_price": {
                "net": net_price,
                "gross": gross_price,
                "tax_rate": tax_rate,
                "price_list": price_list
            },
            "message": f"Deleted {deleted_count} old prices, set new price: {net_price}€ net / {gross_price}€ gross"
        }

    except Exception as e:
        frappe.log_error(f"Force price sync failed for {item_code}: {e}")
        return {
            "success": False,
            "item_code": item_code,
            "message": str(e)
        }


@frappe.whitelist()
def force_reconcile_single_price(item_code: str, price_list: str = None) -> Dict[str, Any]:
    """
    Whitelist wrapper for force_sync_single_product_price.

    Can be called from frontend or console.

    Args:
        item_code: ERPNext Item code
        price_list: Optional price list to use

    Returns:
        Dict with sync result
    """
    return force_sync_single_product_price(item_code=item_code, price_list=price_list)


@temp_shopware_session
def force_sync_all_prices(
    client,
    limit: int = 100,
    offset: int = 0,
    price_list: str = None,
    dry_run: bool = False
) -> Dict[str, Any]:
    """
    Force reconcile ALL product prices - deletes and recreates all prices.

    This is a bulk operation that:
    1. Gets all synced products
    2. For each product: deletes all prices, then sets new price from ERPNext

    Args:
        client: Shopware API client
        limit: Maximum products to process
        offset: Starting offset
        price_list: Price list to use (defaults to Standard-Vertrieb)
        dry_run: If True, only report what would be changed

    Returns:
        Dict with sync statistics
    """
    from frappe.utils import cint

    limit = cint(limit) or 100
    offset = cint(offset) or 0

    if not price_list:
        price_list = "Standard-Vertrieb"

    stats = {
        "processed": 0,
        "updated": 0,
        "skipped": 0,
        "errors": [],
        "price_changes": []
    }

    # Get all synced products
    ecom_items = frappe.db.sql("""
        SELECT ei.erpnext_item_code, ei.integration_item_code
        FROM `tabEcommerce Item` ei
        WHERE ei.integration = 'Shopware6'
        ORDER BY ei.erpnext_item_code
        LIMIT %s OFFSET %s
    """, (limit, offset), as_dict=True)

    currency_id = get_cached_currency_id(client, "EUR")

    for ei in ecom_items:
        item_code = ei.erpnext_item_code
        shopware_id = ei.integration_item_code
        stats["processed"] += 1

        try:
            # Get current Shopware price
            product_response = client.request_get(f"product/{shopware_id}")
            current_prices = product_response.get("data", {}).get("price", [])
            current_net = current_prices[0].get("net", 0) if current_prices else 0

            # Get ERPNext price
            new_net = get_item_price(item_code, price_list)
            if new_net <= 0:
                new_net = get_item_price(item_code)

            tax_rate = get_item_tax_rate(item_code)

            # CRITICAL: Skip items without valid price - never use 0.01 fallback
            if new_net <= 0:
                stats["skipped"] += 1
                stats["errors"].append({
                    "item_code": item_code,
                    "error": "No valid price in ERPNext - skipped to prevent 0.01 sync"
                })
                continue

            new_gross = round(new_net * (1 + tax_rate / 100), 2)

            # Check if price changed
            price_diff = abs(current_net - new_net)

            if price_diff > 0.01:  # Price changed
                stats["price_changes"].append({
                    "item_code": item_code,
                    "old_net": current_net,
                    "new_net": new_net,
                    "diff": round(new_net - current_net, 2)
                })

                if not dry_run:
                    # Delete existing advanced prices
                    prices_response = client.request_post("search/product-price", {
                        "filter": [{"type": "equals", "field": "productId", "value": shopware_id}],
                        "limit": 100
                    })
                    for price_entry in prices_response.get("data", []):
                        price_id = price_entry.get("id")
                        if price_id:
                            try:
                                client.request_delete(f"product-price/{price_id}")
                            except Exception:
                                pass

                    # Update base price
                    price_payload = [{
                        "currencyId": currency_id,
                        "gross": new_gross,
                        "net": new_net,
                        "linked": False,
                    }]
                    client.request_patch(f"product/{shopware_id}", {"price": price_payload})

                stats["updated"] += 1
            else:
                stats["skipped"] += 1

            # Commit periodically
            if stats["processed"] % 20 == 0:
                frappe.db.commit()
                frappe.logger().info(
                    f"[Force Price Sync] Progress: {stats['processed']}/{len(ecom_items)} "
                    f"(updated: {stats['updated']}, skipped: {stats['skipped']})"
                )

        except Exception as e:
            stats["errors"].append({"item_code": item_code, "error": str(e)[:200]})

    frappe.db.commit()

    return {
        "success": True,
        "dry_run": dry_run,
        "price_list": price_list,
        "statistics": stats,
        "message": f"Processed {stats['processed']}: {stats['updated']} updated, {stats['skipped']} unchanged, {len(stats['errors'])} errors" + (" (DRY RUN)" if dry_run else "")
    }


@frappe.whitelist()
def enqueue_force_sync_all_prices(
    batch_size: int = 500,
    price_list: str = None,
    dry_run: bool = False
) -> Dict[str, Any]:
    """
    Enqueue forced price reconciliation as background job.

    Args:
        batch_size: Products per batch
        price_list: Price list to use
        dry_run: If True, only report changes

    Returns:
        Job info dict
    """
    from frappe.utils import cint, now

    batch_size = cint(batch_size) or 500
    dry_run = dry_run in [True, "true", "True", 1, "1"]

    # Count total products
    total = frappe.db.count("Ecommerce Item", {"integration": "Shopware6"})

    job_name = f"shopware6_force_price_sync_{now()}"

    frappe.enqueue(
        "ecommerce_integrations.shopware6.export.price_handler._run_force_price_sync_batched",
        queue="long",
        job_name=job_name,
        timeout=3600 * 4,
        batch_size=batch_size,
        total=total,
        price_list=price_list,
        dry_run=dry_run
    )

    return {
        "success": True,
        "message": f"Force price sync enqueued for {total} products (batch: {batch_size})" + (" DRY RUN" if dry_run else ""),
        "job_name": job_name
    }


def _run_force_price_sync_batched(
    batch_size: int,
    total: int,
    price_list: str = None,
    dry_run: bool = False
):
    """
    Run FORCED price sync in batches.
    FORCE means: Delete ALL prices and recreate - regardless of whether price changed.
    This ensures only one clean price exists per product.
    """
    from ecommerce_integrations.shopware6.connection import get_shopware_client
    from ecommerce_integrations.shopware6.utils import create_shopware_log

    client = get_shopware_client()
    if not client:
        create_shopware_log(
            status="Error",
            method="force_sync_all_prices",
            message="No Shopware client available"
        )
        return

    if not price_list:
        price_list = "Standard-Vertrieb"

    stats = {"processed": 0, "updated": 0, "deleted_prices": 0, "errors": 0}
    num_batches = (total + batch_size - 1) // batch_size

    create_shopware_log(
        status="Queued",
        method="force_sync_all_prices",
        message=f"Starting FORCED price sync for {total} products in {num_batches} batches (delete all + recreate)"
    )

    currency_id = get_cached_currency_id(client, "EUR")

    for batch_num in range(num_batches):
        offset = batch_num * batch_size

        items = frappe.db.sql("""
            SELECT ei.erpnext_item_code, ei.integration_item_code
            FROM `tabEcommerce Item` ei
            WHERE ei.integration = 'Shopware6'
            ORDER BY ei.erpnext_item_code
            LIMIT %s OFFSET %s
        """, (batch_size, offset), as_dict=True)

        for ei in items:
            item_code = ei.erpnext_item_code
            shopware_id = ei.integration_item_code

            try:
                # Get ERPNext price
                new_net = get_item_price(item_code, price_list)
                if new_net <= 0:
                    new_net = get_item_price(item_code)

                tax_rate = get_item_tax_rate(item_code)

                # CRITICAL: Skip items without valid price - never use 0.01 fallback
                if new_net <= 0:
                    stats["errors"] += 1
                    stats["processed"] += 1
                    frappe.logger().warning(
                        f"[Force Price Sync] Skipping {item_code}: No valid price in ERPNext"
                    )
                    continue

                new_gross = round(new_net * (1 + tax_rate / 100), 2)

                if not dry_run:
                    # ALWAYS delete ALL advanced/rule prices
                    prices_response = client.request_post("search/product-price", {
                        "filter": [{"type": "equals", "field": "productId", "value": shopware_id}],
                        "limit": 100
                    })
                    deleted = 0
                    for price_entry in prices_response.get("data", []):
                        try:
                            client.request_delete(f"product-price/{price_entry.get('id')}")
                            deleted += 1
                        except Exception:
                            pass
                    stats["deleted_prices"] += deleted

                    # ALWAYS set fresh price
                    client.request_patch(f"product/{shopware_id}", {
                        "price": [{
                            "currencyId": currency_id,
                            "gross": new_gross,
                            "net": new_net,
                            "linked": False,
                        }]
                    })

                stats["updated"] += 1
                stats["processed"] += 1

            except Exception as e:
                stats["errors"] += 1
                stats["processed"] += 1
                frappe.log_error(f"Force price sync error for {item_code}: {e}")

        frappe.db.commit()

        pct = round(stats["processed"] / total * 100, 1)
        create_shopware_log(
            status="Success",
            method="force_sync_all_prices",
            message=f"Batch {batch_num + 1}/{num_batches} ({pct}%) - Updated: {stats['updated']}, Deleted prices: {stats['deleted_prices']}"
        )

    create_shopware_log(
        status="Success",
        method="force_sync_all_prices",
        message=f"COMPLETED - {stats['updated']} products updated, {stats['deleted_prices']} old prices deleted, {stats['errors']} errors" + (" (DRY RUN)" if dry_run else "")
    )
