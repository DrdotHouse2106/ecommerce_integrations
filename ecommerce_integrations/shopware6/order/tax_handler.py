"""
Shopware 6 Tax Handler

Handles tax calculation and mapping for orders.
"""

from typing import Any, Dict, Optional

import frappe
from frappe.utils import flt


def add_order_taxes(
    so: "frappe.Document",
    order_data: Dict[str, Any],
    setting,
    tax_status: Optional[str] = None,
) -> None:
    """
    Add taxes to the Sales Order.

    The tax handling depends on the Shopware order's taxStatus:
    - "net": B2B order, item prices are net - add tax "On Net Total"
    - "gross": B2C order, item prices include tax - add tax as "Actual"
    - "tax-free": No tax - skip adding taxes

    The included_in_print_rate field is set based on taxStatus:
    - "net": included_in_print_rate = 0 (prices shown are net, tax is added)
    - "gross": included_in_print_rate = 1 (prices shown include tax)

    Args:
        so: Sales Order document
        order_data: Shopware order data
        setting: Shopware Setting document
        tax_status: Shopware order taxStatus ("net", "gross", or "tax-free")
    """
    # Skip tax handling for tax-free orders
    if tax_status == "tax-free":
        return

    # Determine if prices are net based on taxStatus
    is_net_pricing = (tax_status == "net")

    # Collect unique tax rates and their amounts from line items
    tax_data = collect_tax_data(order_data)

    # Find mapped accounts for each tax rate
    map_tax_accounts(tax_data, setting)

    # Create tax entries
    for tax_rate, data in tax_data.items():
        if data["amount"] <= 0:
            continue

        if not data["account"]:
            continue

        if is_net_pricing:
            # B2B mode (net): Item prices are net - add tax "On Net Total"
            # included_in_print_rate = 0 means prices are net, tax will be added
            so.append(
                "taxes",
                {
                    "charge_type": "On Net Total",
                    "account_head": data["account"],
                    "rate": tax_rate,
                    "description": f"VAT {tax_rate}%",
                    "cost_center": setting.cost_center,
                    "included_in_print_rate": 0,
                },
            )
        else:
            # B2C mode (gross): Item prices include tax
            # Use "On Net Total" with included_in_print_rate = 1
            # This tells ERPNext that item prices are gross and tax is included
            # Note: "Actual" charge type cannot be used with included_in_print_rate = 1
            so.append(
                "taxes",
                {
                    "charge_type": "On Net Total",
                    "account_head": data["account"],
                    "rate": tax_rate,
                    "description": f"VAT {tax_rate}%",
                    "cost_center": setting.cost_center,
                    "included_in_print_rate": 1,
                },
            )


def collect_tax_data(order_data: Dict[str, Any]) -> Dict[float, Dict]:
    """
    Collect unique tax rates and their amounts from order data.

    Args:
        order_data: Shopware order data

    Returns:
        Dict mapping tax_rate to {"amount": float, "account": None}
    """
    tax_data = {}

    # Collect from line items
    for line_item in (order_data.get("lineItems") or []):
        price = line_item.get("price", {})
        calculated_taxes = price.get("calculatedTaxes", [])

        for tax in calculated_taxes:
            tax_rate = flt(tax.get("taxRate", 0))
            tax_amount = flt(tax.get("tax", 0))

            if tax_rate not in tax_data:
                tax_data[tax_rate] = {"amount": 0, "account": None}
            tax_data[tax_rate]["amount"] += tax_amount

    # Add shipping taxes
    for delivery in (order_data.get("deliveries") or []):
        shipping_costs = delivery.get("shippingCosts", {})
        calculated_taxes = shipping_costs.get("calculatedTaxes", [])

        for tax in calculated_taxes:
            tax_rate = flt(tax.get("taxRate", 0))
            tax_amount = flt(tax.get("tax", 0))

            if tax_rate not in tax_data:
                tax_data[tax_rate] = {"amount": 0, "account": None}
            tax_data[tax_rate]["amount"] += tax_amount

    return tax_data


def map_tax_accounts(tax_data: Dict[float, Dict], setting) -> None:
    """
    Map tax rates to ERPNext tax accounts.

    Args:
        tax_data: Dict from collect_tax_data
        setting: Shopware Setting document
    """
    for tax_rate in tax_data:
        for tax_mapping in setting.taxes:
            if flt(tax_mapping.shopware_tax) == tax_rate:
                tax_data[tax_rate]["account"] = tax_mapping.tax_account
                break

        if not tax_data[tax_rate]["account"]:
            tax_data[tax_rate]["account"] = setting.default_sales_tax_account


def get_tax_account_for_rate(tax_rate: float, setting) -> str:
    """
    Get the ERPNext tax account for a given tax rate.

    Args:
        tax_rate: Tax rate percentage
        setting: Shopware Setting document

    Returns:
        Tax account name
    """
    for tax_mapping in setting.taxes:
        if flt(tax_mapping.shopware_tax) == tax_rate:
            return tax_mapping.tax_account

    return setting.default_sales_tax_account
