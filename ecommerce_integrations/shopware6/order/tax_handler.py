"""
Shopware 6 Tax Handler

Handles tax calculation and mapping for orders.
"""

from typing import Any

import frappe
from frappe.utils import flt


def add_order_taxes(
    so: "frappe.Document",
    order_data: dict[str, Any],
    setting,
    tax_status: str | None = None,
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
    map_tax_accounts(tax_data, setting, so.get("taxes_and_charges"))

    # One row per account: when several rates share an account (destination
    # country taxation, standard + reduced rate) the line's item tax template
    # carries the actual rate; two rows on one account would tax every line twice.
    rows: dict[str, float] = {}
    for tax_rate, data in tax_data.items():
        if data["amount"] > 0 and data["account"]:
            rows[data["account"]] = max(rows.get(data["account"], 0), tax_rate)

    # Create tax entries
    for account, tax_rate in rows.items():
        data = {"account": account}

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


def collect_tax_data(order_data: dict[str, Any]) -> dict[float, dict]:
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


def map_tax_accounts(tax_data: dict[float, dict], setting, template: str | None = None) -> None:
    """Map tax rates to ERPNext tax accounts (see ``account_for_rate``)."""
    for tax_rate in tax_data:
        tax_data[tax_rate]["account"] = account_for_rate(tax_rate, setting, template)


def account_for_rate(tax_rate: float, setting, template: str | None = None) -> str | None:
    """Tax account for a Shopware rate.

    A sales tax template with a single tax account (e.g. one per destination
    country) takes every rate; otherwise a template row with exactly this rate,
    then the rate mapping of the Shopware Setting, then its default account.
    """
    if template:
        rows = frappe.get_all(
            "Sales Taxes and Charges",
            filters={"parent": template, "parenttype": "Sales Taxes and Charges Template"},
            fields=["account_head", "rate"],
        )
        accounts = {r.account_head for r in rows}
        if len(accounts) == 1:
            return accounts.pop()
        for r in rows:
            if flt(r.rate) and flt(r.rate) == flt(tax_rate):
                return r.account_head
    return get_tax_account_for_rate(tax_rate, setting)


def item_tax_template_for_rate(tax_rate: float, setting, template: str | None = None) -> str | None:
    """Item Tax Template that charges ``tax_rate`` on the account of that rate.

    Without it ERPNext applies every tax row of the order to every line, so a
    basket with 19 % and 7 % items would be taxed with both rates.
    """
    account = account_for_rate(tax_rate, setting, template)
    if not account:
        return None
    parents = frappe.get_all(
        "Item Tax Template Detail",
        filters={"parenttype": "Item Tax Template", "tax_type": account, "tax_rate": flt(tax_rate)},
        pluck="parent",
    )
    if not parents:
        return None
    found = frappe.get_all(
        "Item Tax Template",
        filters={"name": ["in", parents], "disabled": 0, "company": setting.company},
        pluck="name",
        order_by="name asc",
        limit=1,
    )
    return found[0] if found else None


def rate_parts(calculated_taxes: list[dict] | None, total: float) -> list[tuple[float | None, float]]:
    """Split a line total by tax rate: ``[(rate, amount), ...]``.

    Shopware spreads promotions and shipping over the rates of the basket; such
    a line becomes one ERPNext line per rate. Lines with one or no rate stay whole.
    """
    taxes = [t for t in (calculated_taxes or []) if t.get("taxRate") is not None]
    if len(taxes) <= 1:
        return [(flt(taxes[0]["taxRate"]) if taxes else None, flt(total))]
    sign = -1 if flt(total) < 0 else 1
    return [(flt(t["taxRate"]), sign * abs(flt(t.get("price")))) for t in taxes]


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
