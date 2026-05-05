"""Synthetic factories for Smart Collection resolver tests.

Builds ``Item``, ``Item Group`` and ``Item Ecommerce Property`` rows with
predictable, prefix-based names so suites can run in parallel and clean up
deterministically. Inserts use ``ignore_permissions`` and skip duplicates.
All identifiers are generic — never use real brands, customers or codes here.
"""

import uuid

import frappe


def _uniq(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def make_item_group(
    name: str | None = None,
    parent: str = "All Item Groups",
    is_group: int = 0,
) -> str:
    n = name or _uniq("TGroup")
    if frappe.db.exists("Item Group", n):
        return n
    frappe.get_doc(
        {
            "doctype": "Item Group",
            "item_group_name": n,
            "parent_item_group": parent,
            "is_group": is_group,
        }
    ).insert(ignore_permissions=True)
    return n


def make_item(
    item_code: str | None = None,
    item_group: str = "All Item Groups",
    disabled: int = 0,
    is_sales_item: int = 1,
    published_in_website: int = 1,
    manufacturer: str | None = None,
    brand: str | None = None,
    properties: dict[str, str] | None = None,
) -> str:
    code = item_code or _uniq("TItem")
    if frappe.db.exists("Item", code):
        return code

    doc_data: dict = {
        "doctype": "Item",
        "item_code": code,
        "item_name": code,
        "item_group": item_group,
        "disabled": disabled,
        "is_sales_item": is_sales_item,
        "published_in_website": published_in_website,
        "stock_uom": "Nos",
    }
    if manufacturer:
        doc_data["manufacturer"] = manufacturer
    if brand:
        doc_data["brand"] = brand

    doc = frappe.get_doc(doc_data).insert(ignore_permissions=True)

    if properties:
        for prop_name, prop_value in properties.items():
            child = frappe.get_doc(
                {
                    "doctype": "Item Ecommerce Property",
                    "parent": doc.name,
                    "parenttype": "Item",
                    "parentfield": "ecommerce_properties",
                    "property_name": prop_name,
                    "property_value": prop_value,
                }
            )
            child.insert(ignore_permissions=True)
    return doc.name


def cleanup_test_data(prefixes: tuple[str, ...] = ("TItem", "TGroup")) -> None:
    """Delete all Items / Item Groups whose name starts with any test prefix."""
    for doctype in ("Item", "Item Group"):
        names = frappe.get_all(doctype, filters={"name": ("like", "T%")}, pluck="name")
        for n in names:
            if any(n.startswith(p) for p in prefixes):
                try:
                    frappe.delete_doc(doctype, n, force=True, ignore_permissions=True)
                except Exception:
                    # A test row may already be gone if a sibling test cleaned it up.
                    pass
