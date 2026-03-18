"""Access helpers for AI description endpoints."""

import frappe


def require_ai_admin() -> None:
    """Restrict administrative AI actions to system managers."""
    frappe.only_for("System Manager")


def get_item_with_permission(item_code: str, permission_type: str = "write"):
    """Load an item and ensure the current user has the requested permission."""
    item = frappe.get_doc("Item", item_code)
    item.check_permission(permission_type)
    return item


def clamp_limit(limit: int, minimum: int = 1, maximum: int = 500) -> int:
    """Clamp user-provided limits to a safe range."""
    return max(minimum, min(int(limit), maximum))
