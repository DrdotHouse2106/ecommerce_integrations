"""Access helpers for RAG endpoints."""

import frappe


def require_rag_admin() -> None:
    """Restrict administrative RAG actions to system managers."""
    frappe.only_for("System Manager")
