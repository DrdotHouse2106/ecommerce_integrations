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
        price_list: Price list to use (optional)

    Returns:
        Price as float
    """
    # Try standard_rate first
    item = frappe.get_doc("Item", item_code)
    price = flt(item.get(ITEM_SELLING_RATE_FIELD) or 0)

    if price > 0:
        return price

    # Try Item Price table
    filters = {"item_code": item_code, "selling": 1, "price_list_rate": [">", 0]}
    if price_list:
        filters["price_list"] = price_list

    item_price = frappe.db.get_value(
        "Item Price",
        filters,
        "price_list_rate",
        order_by="price_list_rate desc"
    )

    return flt(item_price) if item_price else 0.0


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
    """
    gross_price = round(net_price * (1 + tax_rate / 100), 2) if net_price > 0 else 0.01
    net_price = net_price if net_price > 0 else 0.01

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
