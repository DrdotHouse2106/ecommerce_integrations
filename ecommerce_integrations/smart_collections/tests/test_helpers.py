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


def root_item_group() -> str:
    """Return the locale-appropriate root Item Group name.

    Vanilla ERPNext ships ``All Item Groups``, but localised installs may
    rename it (e.g. German ``Alle Artikelgruppen``). Tests that don't care
    about a specific parent use this so they work on either locale.
    """
    if frappe.db.exists("Item Group", "All Item Groups"):
        return "All Item Groups"
    name = frappe.db.get_value(
        "Item Group", {"is_group": 1}, "name", order_by="lft asc"
    )
    if not name:
        raise RuntimeError("No root Item Group found on this site")
    return name


def default_stock_uom() -> str:
    """Pick any existing UOM for test items. Vanilla ERPNext ships ``Nos``,
    German installs may not — fall back to whatever UOM exists."""
    for candidate in ("Nos", "Stk", "Einheit"):
        if frappe.db.exists("UOM", candidate):
            return candidate
    name = frappe.db.get_value("UOM", {}, "name")
    if not name:
        raise RuntimeError("No UOM found on this site")
    return name


def make_item_group(
    name: str | None = None,
    parent: str | None = None,
    is_group: int = 0,
) -> str:
    n = name or _uniq("TGroup")
    if frappe.db.exists("Item Group", n):
        return n
    frappe.get_doc(
        {
            "doctype": "Item Group",
            "item_group_name": n,
            "parent_item_group": parent or root_item_group(),
            "is_group": is_group,
        }
    ).insert(ignore_permissions=True)
    return n


def make_item(
    item_code: str | None = None,
    item_group: str | None = None,
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
        "item_group": item_group or root_item_group(),
        "disabled": disabled,
        "is_sales_item": is_sales_item,
        "published_in_website": published_in_website,
        "stock_uom": default_stock_uom(),
    }
    if brand:
        doc_data["brand"] = brand

    doc = frappe.get_doc(doc_data).insert(ignore_permissions=True)

    if manufacturer:
        # Manufacturer lives in the ``Item Manufacturer`` child table; the
        # row requires a ``manufacturer_part_no`` so a placeholder is used.
        child = frappe.get_doc(
            {
                "doctype": "Item Manufacturer",
                "parent": doc.name,
                "parenttype": "Item",
                "parentfield": "manufacturers",
                "item_code": doc.name,
                "manufacturer": manufacturer,
                "manufacturer_part_no": f"PN-{doc.name}",
                "is_default": 1,
            }
        )
        child.flags.ignore_mandatory = True
        child.insert(ignore_permissions=True)

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
