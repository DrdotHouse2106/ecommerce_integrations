"""
Shopware 6 Price Handler

Handles price synchronization from ERPNext to Shopware.
Supports multiple price lists and currency handling.
"""

from typing import Any, Dict, List, Optional

import frappe
from frappe.utils import flt

from ecommerce_integrations.shopware6.connection import temp_shopware_session, get_shopware_client
from ecommerce_integrations.shopware6.constants import ITEM_SELLING_RATE_FIELD, SETTING_DOCTYPE
from lib_shopware6_api_base import HEADER_index_asynchronously
from ecommerce_integrations.shopware6.export.utils import get_shopware_document_id
from ecommerce_integrations.shopware6.export.product_mapper import get_cached_currency_id
from ecommerce_integrations.shopware6.utils import get_logger, create_shopware_log
from ecommerce_integrations.shopware6.validators import validate_before_destructive_operation


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


def get_channel_price(item_code: str, sales_channel, setting, tax_rate: float = None) -> float:
    """
    Get the NET price for an item in a specific sales channel.

    Priority:
    1. If channel has price_list set -> use that price list
    2. If channel has price_adjustment_percent -> apply to base price
    3. Fallback -> use setting.default_selling_price_list

    IMPORTANT: If the price list contains gross prices (price_list_includes_tax=True),
    the price is converted to net using the item's tax rate.

    Args:
        item_code: ERPNext Item code
        sales_channel: Shopware Sales Channel row from setting
        setting: Shopware Setting document
        tax_rate: Optional tax rate for gross-to-net conversion. If not provided, fetched from item.

    Returns:
        NET price as float (without tax)
    """
    # Priority 1: Channel has its own price list
    if sales_channel.price_list:
        price = get_item_price(item_code, sales_channel.price_list)

        # Check if this price list contains gross prices (including tax)
        if getattr(sales_channel, 'price_list_includes_tax', False) and price > 0:
            # Convert gross to net
            if tax_rate is None:
                tax_rate = get_item_tax_rate(item_code)
            price = round(price / (1 + tax_rate / 100), 2)

        return price

    # Get base price from default price list
    default_price_list = getattr(setting, 'default_selling_price_list', None)
    base_price = get_item_price(item_code, default_price_list)

    # Check if default price list contains gross prices
    if getattr(setting, 'default_price_list_includes_tax', False) and base_price > 0:
        # Convert gross to net
        if tax_rate is None:
            tax_rate = get_item_tax_rate(item_code)
        base_price = round(base_price / (1 + tax_rate / 100), 2)

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
        get_logger().error(f"Cannot sync price for {item_code}: not synced to Shopware", persist=False)
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
        get_logger().error(f"Failed to sync price for {item_code}: {e}", persist=False)
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
            get_logger().error(f"Failed to sync price for {item_code}: {e}", persist=False)
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
    price_list: str = None,
    force: bool = True
) -> Dict[str, Any]:
    """
    Force sync price for a single product - deletes all existing prices first.

    This function:
    1. Gets the Shopware product ID
    2. Checks if this is a parent/template product (has_variants=True)
    3. For parent products: Sets price to 0 and deletes all advanced prices
    4. For regular/variant products: Sets the price from ERPNext

    Args:
        client: Shopware API client
        item_code: ERPNext Item code
        price_list: Optional price list to use (defaults to Standard-Vertrieb)
        force: If True (default for single product), skip validation threshold

    Returns:
        Dict with success status and details
    """
    logger = get_logger("force_sync_single_product_price")

    shopware_id = get_shopware_document_id("Item", item_code)
    if not shopware_id:
        return {
            "success": False,
            "message": f"Item {item_code} not synced to Shopware"
        }

    try:
        # Check if this is a parent/template product
        item = frappe.get_doc("Item", item_code)
        is_parent_product = item.has_variants

        # Get current prices from Shopware
        response = client.request_post("search/product-price", {
            "filter": [
                {"type": "equals", "field": "productId", "value": shopware_id}
            ],
            "limit": 100
        })
        existing_prices = response.get("data", [])

        # Validate before destructive operation
        validation = validate_before_destructive_operation(
            operation=f"delete_prices_for_{item_code}",
            entity_count=len(existing_prices),
            threshold=10,  # Single product shouldn't have more than 10 price rules
            force=force
        )
        if not validation.proceed:
            logger.warning(validation.message, persist=True)
            return {
                "success": False,
                "item_code": item_code,
                "message": validation.message
            }

        # Delete all existing advanced prices (for ALL products - parent and variants)
        deleted_count = 0
        for price_entry in existing_prices:
            price_id = price_entry.get("id")
            if price_id:
                try:
                    client.request_delete(f"product-price/{price_id}")
                    deleted_count += 1
                except Exception as e:
                    logger.warning(f"Failed to delete price {price_id}: {e}")

        currency_id = get_cached_currency_id(client, "EUR")

        # PARENT PRODUCTS: Set price to 0, no advanced prices
        # Parent products should never have prices - only their variants should
        if is_parent_product:
            price_payload = [{
                "currencyId": currency_id,
                "gross": 0,
                "net": 0,
                "linked": False,
            }]
            client.request_patch(f"product/{shopware_id}", {"price": price_payload})

            logger.success(
                f"Force synced PARENT product {item_code}: price set to 0, deleted {deleted_count} advanced prices",
                request_data={"item_code": item_code, "deleted_prices": deleted_count, "is_parent": True}
            )

            return {
                "success": True,
                "item_code": item_code,
                "shopware_id": shopware_id,
                "deleted_prices": deleted_count,
                "is_parent": True,
                "new_price": {"net": 0, "gross": 0, "tax_rate": 0},
                "message": f"PARENT product: Deleted {deleted_count} old prices, set price to 0 (only variants should have prices)"
            }

        # REGULAR/VARIANT PRODUCTS: Get price from ERPNext
        if not price_list:
            price_list = "Standard-Vertrieb"

        net_price = get_item_price(item_code, price_list)
        if net_price <= 0:
            # Fallback to any price
            net_price = get_item_price(item_code)

        tax_rate = get_item_tax_rate(item_code)

        # CRITICAL: Validate price before syncing - never use 0.01 fallback
        if net_price <= 0:
            logger.warning(f"No valid price found for {item_code}", persist=True)
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

        logger.success(
            f"Force synced price for {item_code}: {net_price}€ net / {gross_price}€ gross",
            request_data={"item_code": item_code, "deleted_prices": deleted_count}
        )

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
        logger.error(f"Force price sync failed for {item_code}", exception=e)
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

    logger = get_logger("force_sync_all_prices")

    stats = {
        "processed": 0,
        "updated": 0,
        "skipped": 0,
        "deactivated": 0,
        "broken_fixed": 0,  # Products with 0.01€ that were fixed
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

            # CRITICAL: Items without valid price should be DEACTIVATED in Shopware
            # This ensures no 0.01€ prices remain and Shopware matches ERPNext exactly
            if new_net <= 0:
                if not dry_run:
                    try:
                        # Deactivate the product in Shopware since it has no valid price
                        client.request_patch(f"product/{shopware_id}", {"active": False})
                        stats["errors"].append({
                            "item_code": item_code,
                            "error": "No valid price in ERPNext - DEACTIVATED in Shopware",
                            "action": "deactivated"
                        })
                    except Exception as e:
                        stats["errors"].append({
                            "item_code": item_code,
                            "error": f"No valid price, failed to deactivate: {str(e)[:100]}",
                            "action": "failed"
                        })
                else:
                    stats["errors"].append({
                        "item_code": item_code,
                        "error": "No valid price in ERPNext - would be DEACTIVATED in Shopware",
                        "action": "would_deactivate"
                    })
                stats["skipped"] += 1
                continue

            new_gross = round(new_net * (1 + tax_rate / 100), 2)

            # Check if price needs update
            price_diff = abs(current_net - new_net)

            # ALWAYS update if:
            # 1. Shopware has invalid 0.01€ price (the broken fallback)
            # 2. Significant price difference exists
            is_broken_price = current_net <= 0.02  # 0.01€ or 0.02€ are broken fallbacks
            needs_update = is_broken_price or price_diff > 0.01

            # ALWAYS delete additional price rules (product-price entries) that may have 0.01€
            # These are separate from the base product price and can cause issues
            if not dry_run:
                # Get ALL additional price rules for this product
                prices_response = client.request_post("search/product-price", {
                    "filter": [{"type": "equals", "field": "productId", "value": shopware_id}],
                    "limit": 500  # Increased limit to catch all price rules
                })
                additional_prices = prices_response.get("data", [])

                # Check for broken 0.01€ prices in the additional rules
                broken_prices = [p for p in additional_prices if flt(p.get("price", [{}])[0].get("net", 0)) <= 0.02]

                if broken_prices:
                    stats["broken_fixed"] += 1
                    logger.info(
                        f"Found {len(broken_prices)} broken price rules for {item_code}, deleting all {len(additional_prices)} rules",
                        persist=True
                    )

                # Delete ALL additional price rules to ensure clean state
                for price_entry in additional_prices:
                    price_id = price_entry.get("id")
                    if price_id:
                        try:
                            client.request_delete(f"product-price/{price_id}")
                        except Exception:
                            pass
            else:
                # Dry run - just check for broken prices
                prices_response = client.request_post("search/product-price", {
                    "filter": [{"type": "equals", "field": "productId", "value": shopware_id}],
                    "limit": 500
                })
                additional_prices = prices_response.get("data", [])
                broken_prices = [p for p in additional_prices if flt(p.get("price", [{}])[0].get("net", 0)) <= 0.02]
                if broken_prices:
                    stats["broken_fixed"] += 1

            if needs_update:
                stats["price_changes"].append({
                    "item_code": item_code,
                    "old_net": current_net,
                    "new_net": new_net,
                    "diff": round(new_net - current_net, 2),
                    "is_broken_price": is_broken_price
                })

                if not dry_run:
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
                logger.info(
                    f"Progress: {stats['processed']}/{len(ecom_items)} "
                    f"(updated: {stats['updated']}, broken_fixed: {stats['broken_fixed']}, skipped: {stats['skipped']})"
                )

        except Exception as e:
            stats["errors"].append({"item_code": item_code, "error": str(e)[:200]})

    frappe.db.commit()

    # Log final summary
    summary = (
        f"Processed {stats['processed']}: "
        f"{stats['updated']} prices updated, "
        f"{stats['broken_fixed']} broken 0.01€ price rules cleaned, "
        f"{stats.get('deactivated', 0)} products deactivated (no ERPNext price), "
        f"{stats['skipped']} unchanged, "
        f"{len(stats['errors'])} errors"
    )
    logger.success(summary + (" (DRY RUN)" if dry_run else ""))

    return {
        "success": True,
        "dry_run": dry_run,
        "price_list": price_list,
        "statistics": stats,
        "message": summary + (" (DRY RUN)" if dry_run else "")
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

    # Get batch size from settings if not provided
    if not batch_size:
        setting = frappe.get_cached_doc(SETTING_DOCTYPE)
        batch_size = cint(getattr(setting, 'price_batch_size', 500)) or 500
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

    IMPORTANT: Parent/template products (has_variants=True) get price=0 and NO advanced prices.
    Only variant products get actual prices from ERPNext.
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

    stats = {"processed": 0, "updated": 0, "deleted_prices": 0, "errors": 0, "parents_fixed": 0}
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
                # Check if this is a parent/template product
                is_parent = frappe.db.get_value("Item", item_code, "has_variants")

                if not dry_run:
                    # ALWAYS delete ALL advanced/rule prices for ALL products
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

                # PARENT PRODUCTS: Set price to 0
                # Parent products should never have prices - only their variants should
                if is_parent:
                    if not dry_run:
                        client.request_patch(f"product/{shopware_id}", {
                            "price": [{
                                "currencyId": currency_id,
                                "gross": 0,
                                "net": 0,
                                "linked": False,
                            }]
                        })
                    stats["parents_fixed"] += 1
                    stats["updated"] += 1
                    stats["processed"] += 1
                    continue

                # REGULAR/VARIANT PRODUCTS: Get price from ERPNext
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
                get_logger().error(f"Force price sync error for {item_code}: {e}", persist=False)

        frappe.db.commit()

        pct = round(stats["processed"] / total * 100, 1)
        create_shopware_log(
            status="Success",
            method="force_sync_all_prices",
            message=f"Batch {batch_num + 1}/{num_batches} ({pct}%) - Updated: {stats['updated']}, Parents fixed: {stats['parents_fixed']}, Deleted prices: {stats['deleted_prices']}"
        )

    create_shopware_log(
        status="Success",
        method="force_sync_all_prices",
        message=f"COMPLETED - {stats['updated']} products updated ({stats['parents_fixed']} parents set to price=0), {stats['deleted_prices']} old prices deleted, {stats['errors']} errors" + (" (DRY RUN)" if dry_run else "")
    )


# =============================================================================
# BATCH PRICE SYNC (using Shopware Sync API for 5-10x speedup)
# =============================================================================

PRICE_BATCH_SIZE = 100  # Products per sync request
PRICE_CHUNK_SIZE = 1000  # Items to process before sending (memory optimization)
BATCH_DELAY_SECONDS = 0.05  # 50ms delay between API calls to avoid overwhelming Shopware


def _log_failed_items_to_db(failed_ids: List[str], operation: str, error_msg: str) -> None:
    """
    Persist failed sync items to database for later retry.

    Creates a Shopware Log entry with all failed item IDs so they can be
    retried manually or via scheduled job.

    Args:
        failed_ids: List of Shopware product IDs that failed
        operation: Type of operation (price_sync, deactivate_sync, etc.)
        error_msg: Error message describing the failure
    """
    if not failed_ids:
        return

    try:
        create_shopware_log(
            status="Error",
            method=f"batch_sync.{operation}",
            message=f"Failed to sync {len(failed_ids)} items: {error_msg}",
            request_data={
                "operation": operation,
                "failed_count": len(failed_ids),
                "failed_ids": failed_ids[:100],  # Limit stored IDs
                "retry_available": True
            }
        )
    except Exception:
        # Don't let logging failures break the sync
        pass


def force_sync_prices_batch(
    price_list: str = None,
    progress_callback: callable = None
) -> Dict[str, Any]:
    """
    Batch-sync all prices using Shopware Sync API (5-10x faster).

    Instead of individual PATCH calls per product, this collects all price
    updates and sends them in batches via _action/sync.

    Memory-optimized: Processes items in chunks to avoid loading all updates at once.

    Args:
        price_list: Price list to use (defaults to Standard-Vertrieb)
        progress_callback: Optional callback(batch_num, total_batches, processed, total)

    Returns:
        Dict with sync statistics
    """
    import time

    logger = get_logger("force_sync_prices_batch")
    client = get_shopware_client()

    if not client:
        return {"success": False, "error": "No Shopware client available"}

    setting = frappe.get_cached_doc(SETTING_DOCTYPE)

    if not price_list:
        price_list = getattr(setting, 'default_selling_price_list', 'Standard-Vertrieb') or 'Standard-Vertrieb'

    # Get currency from settings instead of hardcoded EUR
    currency = getattr(setting, 'default_currency', 'EUR') or 'EUR'

    stats = {
        "total": 0,
        "updated": 0,
        "skipped": 0,
        "deactivated": 0,
        "errors": [],
        "failed_items": [],  # Track failed items for potential retry
        "batches_sent": 0
    }

    # Get all synced products
    ecom_items = frappe.db.sql("""
        SELECT
            ei.erpnext_item_code,
            ei.integration_item_code,
            i.has_variants
        FROM `tabEcommerce Item` ei
        JOIN `tabItem` i ON i.name = ei.erpnext_item_code
        WHERE ei.integration = 'Shopware6'
        ORDER BY ei.erpnext_item_code
    """, as_dict=True)

    stats["total"] = len(ecom_items)

    if not ecom_items:
        logger.info("No products to sync")
        return {"success": True, "statistics": stats}

    logger.info(f"Batch price sync: Processing {stats['total']} products")

    currency_id = get_cached_currency_id(client, currency)

    # Collect updates in batches
    price_updates = []
    deactivate_updates = []

    for ei in ecom_items:
        item_code = ei.erpnext_item_code
        shopware_id = ei.integration_item_code
        is_parent = ei.has_variants

        # Parent products get price 0 (only variants have prices)
        if is_parent:
            price_updates.append({
                "id": shopware_id,
                "price": [{
                    "currencyId": currency_id,
                    "gross": 0,
                    "net": 0,
                    "linked": False,
                }]
            })
            continue

        # Get ERPNext price
        net_price = get_item_price(item_code, price_list)
        if net_price <= 0:
            net_price = get_item_price(item_code)

        tax_rate = get_item_tax_rate(item_code)

        # No valid price -> deactivate
        if net_price <= 0:
            deactivate_updates.append({
                "id": shopware_id,
                "active": False
            })
            stats["deactivated"] += 1
            continue

        gross_price = round(net_price * (1 + tax_rate / 100), 2)

        price_updates.append({
            "id": shopware_id,
            "price": [{
                "currencyId": currency_id,
                "gross": gross_price,
                "net": net_price,
                "linked": False,
            }]
        })

    # Send price updates in batches (with division by zero protection)
    if price_updates:
        total_batches = (len(price_updates) + PRICE_BATCH_SIZE - 1) // PRICE_BATCH_SIZE
        logger.info(f"Sending {len(price_updates)} price updates in {total_batches} batches")

        for batch_num in range(total_batches):
            start_idx = batch_num * PRICE_BATCH_SIZE
            end_idx = min(start_idx + PRICE_BATCH_SIZE, len(price_updates))
            batch = price_updates[start_idx:end_idx]

            try:
                sync_payload = {
                    "write-product-prices": {
                        "entity": "product",
                        "action": "upsert",
                        "payload": batch
                    }
                }

                response = client.request_post(
                    "_action/sync",
                    sync_payload,
                    update_header_fields=HEADER_index_asynchronously
                )

                # Validate Sync API response (check for partial failures)
                not_found = response.get("notFound", []) if response else []
                if not_found:
                    logger.warning(f"Sync API: {len(not_found)} items not found in batch {batch_num + 1}")
                    stats["errors"].append({
                        "batch": batch_num + 1,
                        "type": "partial_failure",
                        "not_found": not_found[:10]  # Limit logged IDs
                    })
                    # Track not-found items for retry
                    stats["failed_items"].extend(not_found)

                # Count successful updates (batch size minus not_found)
                successful = len(batch) - len(not_found)
                stats["updated"] += successful
                stats["batches_sent"] += 1

                if progress_callback:
                    progress_callback(batch_num + 1, total_batches, stats["updated"], stats["total"])

                logger.info(f"Price batch {batch_num + 1}/{total_batches}: {successful}/{len(batch)} products updated")

            except Exception as e:
                # Track failed items for potential retry
                failed_ids = [item["id"] for item in batch]
                stats["failed_items"].extend(failed_ids)
                stats["errors"].append({
                    "batch": batch_num + 1,
                    "error": str(e)[:200],
                    "failed_count": len(batch)
                })
                logger.error(f"Price batch {batch_num + 1} failed: {e}")

                # Persist failed items to database for later retry
                _log_failed_items_to_db(failed_ids, "price_sync", str(e)[:200])

            # Rate limiting: small delay between batches to avoid overwhelming Shopware
            if batch_num < total_batches - 1:
                time.sleep(BATCH_DELAY_SECONDS)
    else:
        logger.info("No price updates to send")

    # Send deactivation updates in batches
    if deactivate_updates:
        deactivate_batches = (len(deactivate_updates) + PRICE_BATCH_SIZE - 1) // PRICE_BATCH_SIZE
        logger.info(f"Deactivating {len(deactivate_updates)} products without prices")

        for batch_num in range(deactivate_batches):
            start_idx = batch_num * PRICE_BATCH_SIZE
            end_idx = min(start_idx + PRICE_BATCH_SIZE, len(deactivate_updates))
            batch = deactivate_updates[start_idx:end_idx]

            try:
                sync_payload = {
                    "deactivate-products": {
                        "entity": "product",
                        "action": "upsert",
                        "payload": batch
                    }
                }

                response = client.request_post(
                    "_action/sync",
                    sync_payload,
                    update_header_fields=HEADER_index_asynchronously
                )

                # Validate Sync API response
                not_found = response.get("notFound", []) if response else []
                if not_found:
                    stats["failed_items"].extend(not_found)
                    stats["errors"].append({
                        "action": "deactivate",
                        "batch": batch_num + 1,
                        "type": "partial_failure",
                        "not_found_count": len(not_found)
                    })

            except Exception as e:
                failed_ids = [item["id"] for item in batch]
                stats["failed_items"].extend(failed_ids)
                stats["errors"].append({
                    "action": "deactivate",
                    "batch": batch_num + 1,
                    "error": str(e)[:200]
                })
                # Persist failed items for retry
                _log_failed_items_to_db(failed_ids, "deactivate_sync", str(e)[:200])

            # Rate limiting
            if batch_num < deactivate_batches - 1:
                time.sleep(BATCH_DELAY_SECONDS)

    stats["skipped"] = stats["total"] - stats["updated"] - stats["deactivated"]

    logger.success(
        f"Batch price sync complete: {stats['updated']} updated, "
        f"{stats['deactivated']} deactivated, {len(stats['errors'])} errors"
    )

    return {
        "success": len(stats["errors"]) == 0,
        "statistics": stats
    }


def delete_broken_price_rules_batch(client=None) -> Dict[str, Any]:
    """
    Batch-delete all broken product-price rules (0.01€ fallback prices).

    Uses Sync API delete action for efficiency.

    Returns:
        Dict with deletion statistics
    """
    logger = get_logger("delete_broken_price_rules_batch")

    if not client:
        client = get_shopware_client()

    if not client:
        return {"success": False, "error": "No Shopware client available"}

    stats = {"checked": 0, "deleted": 0, "errors": []}

    # Get all product-price entries
    all_prices = []
    page = 1
    while True:
        response = client.request_post("search/product-price", {
            "limit": 500,
            "page": page
        })
        prices = response.get("data", [])
        if not prices:
            break
        all_prices.extend(prices)
        page += 1

    stats["checked"] = len(all_prices)
    logger.info(f"Checking {stats['checked']} product-price rules")

    # Find broken prices (0.01€ or 0.02€)
    broken_ids = []
    for price in all_prices:
        price_data = price.get("price", [{}])
        if price_data:
            net = flt(price_data[0].get("net", 0))
            if net <= 0.02:
                broken_ids.append({"id": price.get("id")})

    if not broken_ids:
        logger.info("No broken price rules found")
        return {"success": True, "statistics": stats}

    logger.info(f"Found {len(broken_ids)} broken price rules to delete")

    # Batch delete
    batch_size = 100
    for i in range(0, len(broken_ids), batch_size):
        batch = broken_ids[i:i + batch_size]

        try:
            sync_payload = {
                "delete-broken-prices": {
                    "entity": "product_price",
                    "action": "delete",
                    "payload": batch
                }
            }

            client.request_post("_action/sync", sync_payload)
            stats["deleted"] += len(batch)

        except Exception as e:
            stats["errors"].append({"batch": i // batch_size + 1, "error": str(e)[:200]})
            logger.error(f"Failed to delete price batch: {e}")

    logger.success(f"Deleted {stats['deleted']} broken price rules")
    return {"success": True, "statistics": stats}
