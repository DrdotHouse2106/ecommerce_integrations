"""
Shopware 6 Line Item Handler

Handles processing of order line items (products, shipping, discounts).
"""

from typing import Any, Dict

import frappe
from frappe.utils import flt

from ecommerce_integrations.shopware6.product import get_item_code
from ecommerce_integrations.shopware6.utils import (
    get_logger,
    get_net_unit_price_from_line_item,
    get_tax_rate_from_line_item,
    convert_gross_to_net,
)


def add_order_item(so: "frappe.Document", line_item: Dict[str, Any], setting) -> None:
    """
    Add a line item to the Sales Order.

    Supports both B2B (net prices) and B2C (gross prices) modes.
    When import_prices_as_net is enabled (B2B), Shopware's gross unitPrice
    is converted to net price using the tax rate from the line item.

    Args:
        so: Sales Order document
        line_item: Shopware line item data
        setting: Shopware Setting document
    """
    # Handle different line item types
    item_type = line_item.get("type", "")

    # Handle promotions/discounts (negative line items)
    if item_type in ["promotion", "discount"]:
        _add_discount_item(so, line_item, setting)
        return

    # Skip non-product items (except shipping and discounts handled above)
    if item_type not in ["product", ""]:
        # Handle shipping as item if configured
        if item_type == "shipping" and setting.add_shipping_as_item:
            _add_shipping_item(so, line_item, setting)
        return

    # Get ERPNext item code
    item_code = get_item_code(line_item)
    if not item_code:
        # Try to sync the product
        product_id = line_item.get("productId") or line_item.get("referencedId")
        if product_id:
            from ecommerce_integrations.shopware6.product import ShopwareProduct

            product = ShopwareProduct(product_id)
            product.sync_product()
            item_code = get_item_code(line_item)

    if not item_code:
        logger = get_logger("add_order_item")
        logger.warning(
            f"Could not find/create item for Shopware product: {line_item.get('productId')}",
            persist=True
        )
        return

    # Get price - handle B2B (net) vs B2C (gross) mode
    # Shopware's unitPrice is always gross (including tax)
    use_net_prices = getattr(setting, 'import_prices_as_net', True)
    default_tax_rate = flt(getattr(setting, 'default_tax_rate', 19.0))

    if use_net_prices:
        # B2B mode: Convert gross to net price
        unit_price = flt(get_net_unit_price_from_line_item(line_item, default_tax_rate))
    else:
        # B2C mode: Use gross price as-is
        unit_price = flt(line_item.get("unitPrice", 0))

    quantity = flt(line_item.get("quantity", 1))

    so.append(
        "items",
        {
            "item_code": item_code,
            "qty": quantity,
            "rate": unit_price,
            "warehouse": setting.warehouse,
            "delivery_date": so.delivery_date,
            "description": line_item.get("label", ""),
        },
    )


def _add_shipping_item(so: "frappe.Document", line_item: Dict[str, Any], setting) -> None:
    """Add shipping as a line item.

    Supports both B2B (net prices) and B2C (gross prices) modes.
    """
    if not setting.shipping_item:
        return

    # Handle B2B (net) vs B2C (gross) mode for shipping
    use_net_prices = getattr(setting, 'import_prices_as_net', True)
    default_tax_rate = flt(getattr(setting, 'default_tax_rate', 19.0))

    if use_net_prices:
        # B2B mode: Convert gross to net price
        shipping_price = flt(get_net_unit_price_from_line_item(line_item, default_tax_rate))
    else:
        # B2C mode: Use gross price as-is
        shipping_price = flt(line_item.get("unitPrice", 0))

    if shipping_price > 0:
        so.append(
            "items",
            {
                "item_code": setting.shipping_item,
                "qty": 1,
                "rate": shipping_price,
                "warehouse": setting.warehouse,
                "delivery_date": so.delivery_date,
            },
        )


def _add_discount_item(so: "frappe.Document", line_item: Dict[str, Any], setting) -> None:
    """
    Add a discount/promotion as a negative line item.

    Shopware promotions are stored as separate line items with type "promotion" or "discount"
    and typically have negative prices. This creates a corresponding negative line item
    in ERPNext to reflect the discount.

    Args:
        so: Sales Order document
        line_item: Shopware discount/promotion line item data
        setting: Shopware Setting document
    """
    # Get discount item from settings or use a default
    discount_item = getattr(setting, 'discount_item', None)
    if not discount_item:
        # Try to find or create a generic discount item
        discount_item = frappe.db.get_value("Item", {"item_code": "DISCOUNT"}, "name")
        if not discount_item:
            # Log and skip if no discount item configured
            logger = get_logger("add_discount_item")
            logger.warning(
                f"Discount line item found in Shopware order but no discount item configured. "
                f"Discount label: {line_item.get('label')}, Amount: {line_item.get('totalPrice')}",
                persist=True
            )
            return

    # Get discount price - discounts typically have negative total price
    # Use totalPrice instead of unitPrice for promotions as it may be more accurate
    discount_price = flt(line_item.get("totalPrice", 0))

    # If not negative (Shopware may already store as negative), make it negative
    if discount_price > 0:
        discount_price = -discount_price

    # Handle B2B (net) vs B2C (gross) mode for discounts
    use_net_prices = getattr(setting, 'import_prices_as_net', True)
    default_tax_rate = flt(getattr(setting, 'default_tax_rate', 19.0))

    if use_net_prices and discount_price != 0:
        # B2B mode: Convert gross to net price
        tax_rate = get_tax_rate_from_line_item(line_item, default_tax_rate)
        discount_price = convert_gross_to_net(discount_price, tax_rate)

    # Get discount label/description
    label = line_item.get("label", "") or line_item.get("description", "") or "Rabatt"

    if discount_price != 0:
        so.append(
            "items",
            {
                "item_code": discount_item,
                "qty": 1,
                "rate": discount_price,  # Negative value for discount
                "warehouse": setting.warehouse,
                "delivery_date": so.delivery_date,
                "description": label,
            },
        )
