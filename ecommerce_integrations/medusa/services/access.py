"""Access helpers for Medusa service endpoints."""

import frappe


def require_medusa_admin() -> None:
    """Restrict Medusa maintenance and admin actions to system managers."""
    frappe.only_for("System Manager")
