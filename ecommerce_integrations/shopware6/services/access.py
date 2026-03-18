"""Access helpers for Shopware service endpoints."""

import frappe


def require_shopware_admin() -> None:
    """Restrict Shopware maintenance and admin actions to system managers."""
    frappe.only_for("System Manager")


def require_item_write_permission(item_code: str):
    """Ensure the current user may modify the referenced item."""
    item = frappe.get_doc("Item", item_code)
    item.check_permission("write")
    return item
