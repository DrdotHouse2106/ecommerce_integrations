"""
Shopware 6 Tax Handler

Handles tax calculation and mapping for orders.
"""

from typing import Any, Dict

import frappe
from frappe.utils import flt


def add_order_taxes(so: "frappe.Document", order_data: Dict[str, Any], setting) -> None:
    """
    Add taxes to the Sales Order.

    When import_prices_as_net is enabled (B2B mode), taxes are added as "On Net Total"
    with the tax rate as percentage. This ensures all items are taxed correctly.

    When import_prices_as_net is disabled (B2C mode), taxes are added as "Actual"
    with the exact amount from Shopware (since prices already include tax).

    Args:
        so: Sales Order document
        order_data: Shopware order data
        setting: Shopware Setting document
    """
    use_net_prices = getattr(setting, 'import_prices_as_net', True)

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

        if use_net_prices:
            # B2B mode: Use "On Net Total" with tax rate as percentage
            # This ensures all items are taxed correctly with the same rate
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
            # B2C mode: Use "Actual" with exact amount (prices include tax)
            so.append(
                "taxes",
                {
                    "charge_type": "Actual",
                    "account_head": data["account"],
                    "tax_amount": data["amount"],
                    "description": f"VAT {tax_rate}%",
                    "cost_center": setting.cost_center,
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
    for line_item in order_data.get("lineItems", []):
        price = line_item.get("price", {})
        calculated_taxes = price.get("calculatedTaxes", [])

        for tax in calculated_taxes:
            tax_rate = flt(tax.get("taxRate", 0))
            tax_amount = flt(tax.get("tax", 0))

            if tax_rate not in tax_data:
                tax_data[tax_rate] = {"amount": 0, "account": None}
            tax_data[tax_rate]["amount"] += tax_amount

    # Add shipping taxes
    for delivery in order_data.get("deliveries", []):
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
