"""
Shopware 6 Line Item Handler

Handles processing of order line items (products, shipping, discounts).
"""

from typing import Any, Dict, Optional

import frappe
from frappe.utils import flt

from ecommerce_integrations.shopware6.product import get_item_code
from ecommerce_integrations.shopware6.utils import (
    get_logger,
    get_net_unit_price_from_line_item,
    get_tax_rate_from_line_item,
    convert_gross_to_net,
)


def add_order_item(
    so: "frappe.Document",
    line_item: Dict[str, Any],
    setting,
    tax_status: Optional[str] = None,
) -> None:
    """
    Add a line item to the Sales Order.

    Supports both B2B (net prices) and B2C (gross prices) modes.
    The price handling depends on the Shopware order's taxStatus:
    - "net": Prices are already net (B2B) - use as-is
    - "gross": Prices include tax (B2C) - convert to net
    - "tax-free": No tax - use as-is

    Args:
        so: Sales Order document
        line_item: Shopware line item data
        setting: Shopware Setting document
        tax_status: Shopware order taxStatus ("net", "gross", or "tax-free")
    """
    # Handle different line item types
    item_type = line_item.get("type", "")

    # Handle promotions/discounts (negative line items)
    if item_type in ["promotion", "discount"]:
        _add_discount_item(so, line_item, setting, tax_status)
        return

    # Skip non-product items (except shipping and discounts handled above)
    if item_type not in ["product", ""]:
        # Handle shipping as item if configured
        if item_type == "shipping" and setting.add_shipping_as_item:
            _add_shipping_item(so, line_item, setting, tax_status)
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

    # Get price based on Shopware order's taxStatus
    # - "net": B2B order, prices are already net - use as-is
    # - "gross": B2C order, prices include tax - keep as gross (ERPNext handles with included_in_print_rate=1)
    # - "tax-free": No tax applied - use as-is
    #
    # IMPORTANT: For B2C (gross), we keep the gross price because tax_handler sets included_in_print_rate=1
    # which tells ERPNext that item.rate already includes tax. ERPNext then calculates net internally.

    # Always use the unitPrice from Shopware as-is
    # For "gross" orders: unitPrice is gross, and included_in_print_rate=1 handles it
    # For "net" orders: unitPrice is net, and included_in_print_rate=0 handles it
    unit_price = flt(line_item.get("unitPrice", 0))

    quantity = flt(line_item.get("quantity", 1))

    item_drop_ship = frappe.db.get_value("Item", item_code, "delivered_by_supplier")
    supplier = None
    if item_drop_ship:
        supplier = frappe.db.get_value(
            "Item Supplier",
            {
                "parent": item_code,
                "parenttype": "Item",
                "parentfield": "supplier_items",
            },
            "supplier",
            order_by="idx asc",
        )

    # Get item_name from Shopware label or fallback to ERPNext Item master
    # Truncate to 140 chars to avoid ERPNext field length error
    item_name = line_item.get("label", "") or frappe.db.get_value("Item", item_code, "item_name") or item_code
    if len(item_name) > 140:
        item_name = item_name[:137] + "..."

    so.append(
        "items",
        {
            "item_code": item_code,
            "item_name": item_name,
            "qty": quantity,
            "rate": unit_price,
            "warehouse": setting.warehouse,
            "delivery_date": so.delivery_date,
            "description": line_item.get("label", ""),
            "delivered_by_supplier": item_drop_ship,
            "supplier": supplier,
        },
    )


def _add_shipping_item(
    so: "frappe.Document",
    line_item: Dict[str, Any],
    setting,
    tax_status: Optional[str] = None,
) -> None:
    """Add shipping as a line item.

    Supports both B2B (net prices) and B2C (gross prices) modes.
    For B2C, keep gross price (ERPNext handles with included_in_print_rate=1).
    """
    if not setting.shipping_item:
        return

    # Use unitPrice as-is (gross for B2C, net for B2B)
    # ERPNext handles the tax inclusion via included_in_print_rate flag
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


def add_shipping_costs(
    so: "frappe.Document",
    order_data: Dict[str, Any],
    setting,
    tax_status: Optional[str] = None,
) -> None:
    """
    Add shipping costs as a line item to the Sales Order.

    Shopware stores shipping costs in the order's shippingCosts field (directly on order)
    or in deliveries[].shippingCosts. This function extracts the shipping cost and adds
    it as a separate line item using the configured shipping item.

    Args:
        so: Sales Order document
        order_data: Shopware order data
        setting: Shopware Setting document
        tax_status: Shopware order taxStatus ("net", "gross", or "tax-free")
    """
    # Check if shipping as line item is enabled
    if not getattr(setting, 'add_shipping_as_item', False):
        return

    # Get shipping item from settings
    shipping_item = getattr(setting, 'shipping_item', None)
    if not shipping_item:
        logger = get_logger("add_shipping_costs")
        logger.warning(
            "add_shipping_as_item is enabled but no shipping_item configured",
            persist=True
        )
        return

    # Check if item exists
    if not frappe.db.exists("Item", shipping_item):
        logger = get_logger("add_shipping_costs")
        logger.warning(
            f"Shipping item '{shipping_item}' does not exist in ERPNext",
            persist=True
        )
        return

    # Get shipping costs from order data
    # First try order-level shippingCosts
    shipping_costs = order_data.get("shippingCosts", {})
    shipping_total = flt(shipping_costs.get("totalPrice", 0))

    # If no order-level shipping, try deliveries
    if not shipping_total:
        for delivery in (order_data.get("deliveries") or []):
            delivery_shipping = delivery.get("shippingCosts", {})
            shipping_total += flt(delivery_shipping.get("totalPrice", 0))

    # Skip if no shipping costs
    if shipping_total <= 0:
        return

    # Use shipping total as-is (gross for B2C, net for B2B)
    # ERPNext handles the tax inclusion via included_in_print_rate flag set by tax_handler
    shipping_price = shipping_total

    # Add shipping as line item
    so.append(
        "items",
        {
            "item_code": shipping_item,
            "qty": 1,
            "rate": shipping_price,
            "warehouse": setting.warehouse,
            "delivery_date": so.delivery_date,
            "description": "Versandkosten / Shipping",
        },
    )


def _add_discount_item(
    so: "frappe.Document",
    line_item: Dict[str, Any],
    setting,
    tax_status: Optional[str] = None,
) -> None:
    """
    Add a discount/promotion as a negative line item.

    Shopware promotions are stored as separate line items with type "promotion" or "discount"
    and typically have negative prices. This creates a corresponding negative line item
    in ERPNext to reflect the discount.

    Args:
        so: Sales Order document
        line_item: Shopware discount/promotion line item data
        setting: Shopware Setting document
        tax_status: Shopware order taxStatus ("net", "gross", or "tax-free")
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
    # Keep as-is (gross for B2C, net for B2B) - ERPNext handles via included_in_print_rate
    discount_price = flt(line_item.get("totalPrice", 0))

    # If not negative (Shopware may already store as negative), make it negative
    if discount_price > 0:
        discount_price = -discount_price

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
