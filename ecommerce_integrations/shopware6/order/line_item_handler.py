"""
Shopware 6 Line Item Handler

Handles processing of order line items (products, shipping, discounts).
"""

from typing import Any

import frappe
from frappe.utils import flt

from ecommerce_integrations.shopware6.order.tax_handler import item_tax_template_for_rate, rate_parts
from ecommerce_integrations.shopware6.product import get_item_code
from ecommerce_integrations.shopware6.utils import (
    get_logger,
)


def add_order_item(
    so: "frappe.Document",
    line_item: dict[str, Any],
    setting,
    tax_status: str | None = None,
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
    decimal_qty = decimal_quantity(line_item, setting)
    if decimal_qty:
        # Plugin-managed decimal quantity (m², m …): the payload holds the
        # line's total amount in the plugin unit (Shopware's own quantity
        # may be 1 or a piece count); the rate is derived so the line
        # total stays exactly Shopware's.
        line_total = flt(line_item.get("totalPrice")) or unit_price * quantity
        quantity = decimal_qty
        unit_price = line_total / quantity

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

    # Auto-heal "Mehrpreis"-style surcharge items: Shopware sells them as
    # standalone line items, but they were imported into ERPNext as
    # ``is_purchase_item=1, is_sales_item=0`` because they are not
    # standalone catalogue products in the ERP's sense. The first time
    # such an item arrives on an order we flip ``is_sales_item=1`` so the
    # Sales Order can be saved — refusing the order would leave the
    # operator with the choice of either toggling the flag manually for
    # every surcharge variant or losing the order entirely.
    _ensure_sales_item(item_code, line_item.get("productId"))

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
            "item_tax_template": _line_tax_template(so, setting, (line_item.get("price") or {}).get("calculatedTaxes")),
        },
    )


def _line_tax_template(so, setting, calculated_taxes) -> str | None:
    """Item Tax Template for a line taxed at exactly one Shopware rate."""
    parts = rate_parts(calculated_taxes, 0)
    if len(parts) != 1 or parts[0][0] is None:
        return None
    return item_tax_template_for_rate(parts[0][0], setting, so.get("taxes_and_charges"))


def _append_by_rate(so, setting, row: dict, calculated_taxes, total: float) -> None:
    """Append ``row`` once per tax rate of the Shopware line, rate = that part."""
    for tax_rate, amount in rate_parts(calculated_taxes, total):
        if not amount:
            continue
        template = (
            item_tax_template_for_rate(tax_rate, setting, so.get("taxes_and_charges"))
            if tax_rate is not None else None
        )
        so.append("items", {**row, "rate": amount, "item_tax_template": template})


def decimal_quantity(line_item: dict[str, Any], setting) -> float:
    """Value at ``decimal_quantity_payload_path`` in the line payload, or 0."""
    path = (getattr(setting, "decimal_quantity_payload_path", None) or "").strip()
    if not path:
        return 0.0
    value: Any = line_item.get("payload") or {}
    for key in path.split("."):
        if not isinstance(value, dict):
            return 0.0
        value = value.get(key)
    if isinstance(value, str):
        value = value.replace(",", ".")
    try:
        return max(float(value or 0), 0.0)
    except (TypeError, ValueError):
        return 0.0


def _ensure_sales_item(item_code: str, shopware_product_id: str | None = None) -> None:
    """Flip ``is_sales_item`` to 1 if needed so the Sales Order can be saved.

    Idempotent: a no-op when the item is already a sales item or doesn't
    exist. Writes via ``db_set`` to skip mandatory-field validation —
    we're only touching this one boolean.
    """
    is_sales = frappe.db.get_value("Item", item_code, "is_sales_item")
    if is_sales is None:  # item doesn't exist — let the upstream code raise
        return
    if is_sales:
        return
    try:
        frappe.db.set_value("Item", item_code, "is_sales_item", 1,
                            update_modified=False)
        get_logger("shopware_order").info(
            f"auto-enabled is_sales_item on {item_code} (Shopware product "
            f"{shopware_product_id or '?'} appeared on an order)",
            persist=True,
            request_data={"item_code": item_code,
                          "shopware_product_id": shopware_product_id},
        )
    except Exception as exc:  # noqa: BLE001 — must never break order import
        get_logger("shopware_order").warning(
            f"failed to auto-enable is_sales_item on {item_code}: {exc}",
            persist=True,
        )


def _add_shipping_item(
    so: "frappe.Document",
    line_item: dict[str, Any],
    setting,
    tax_status: str | None = None,
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
        _append_by_rate(
            so, setting,
            {"item_code": setting.shipping_item, "qty": 1, "warehouse": setting.warehouse,
             "delivery_date": so.delivery_date},
            (line_item.get("price") or {}).get("calculatedTaxes"), shipping_price,
        )


def add_shipping_costs(
    so: "frappe.Document",
    order_data: dict[str, Any],
    setting,
    tax_status: str | None = None,
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
    shipping_taxes = list(shipping_costs.get("calculatedTaxes") or [])

    # If no order-level shipping, try deliveries
    if not shipping_total:
        shipping_taxes = []
        for delivery in (order_data.get("deliveries") or []):
            delivery_shipping = delivery.get("shippingCosts", {})
            shipping_total += flt(delivery_shipping.get("totalPrice", 0))
            shipping_taxes += delivery_shipping.get("calculatedTaxes") or []

    # Skip if no shipping costs
    if shipping_total <= 0:
        return

    # Use shipping total as-is (gross for B2C, net for B2B)
    # ERPNext handles the tax inclusion via included_in_print_rate flag set by tax_handler
    shipping_price = shipping_total

    # Add shipping as line item (one per tax rate when Shopware split it)
    _append_by_rate(
        so, setting,
        {"item_code": shipping_item, "qty": 1, "warehouse": setting.warehouse,
         "delivery_date": so.delivery_date, "description": "Versandkosten / Shipping"},
        _merge_rates(shipping_taxes), shipping_price,
    )


def _merge_rates(calculated_taxes: list[dict]) -> list[dict]:
    """Sum calculatedTaxes of several deliveries per rate."""
    merged: dict[float, dict] = {}
    for t in calculated_taxes:
        rate = flt(t.get("taxRate"))
        m = merged.setdefault(rate, {"taxRate": rate, "price": 0.0, "tax": 0.0})
        m["price"] += flt(t.get("price"))
        m["tax"] += flt(t.get("tax"))
    return list(merged.values())


def _add_discount_item(
    so: "frappe.Document",
    line_item: dict[str, Any],
    setting,
    tax_status: str | None = None,
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
        # Negative line(s); a promotion spread over several rates becomes one line per rate
        _append_by_rate(
            so, setting,
            {"item_code": discount_item, "qty": 1, "warehouse": setting.warehouse,
             "delivery_date": so.delivery_date, "description": label},
            (line_item.get("price") or {}).get("calculatedTaxes"), discount_price,
        )
