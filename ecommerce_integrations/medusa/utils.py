"""Shared utilities for the Medusa integration."""

import frappe

from ecommerce_integrations.medusa.constants import (
    LOG_DOCTYPE,
    MEDUSA_PRICE_FACTOR,
    MODULE_NAME,
    SETTING_DOCTYPE,
)


def is_medusa_enabled() -> bool:
    """Check if Medusa integration is enabled."""
    try:
        return bool(frappe.db.get_single_value(SETTING_DOCTYPE, "enable_medusa"))
    except Exception:
        return False


def get_medusa_setting():
    """Get cached Medusa Setting document."""
    return frappe.get_cached_doc(SETTING_DOCTYPE)


def medusa_price_to_erpnext(amount_in_cents: int) -> float:
    """Convert Medusa price (cents) to ERPNext (standard unit). 4999 → 49.99"""
    if amount_in_cents is None:
        return 0.0
    return round(amount_in_cents / MEDUSA_PRICE_FACTOR, 2)


def erpnext_price_to_medusa(amount: float) -> int:
    """Convert ERPNext price to Medusa (cents). 49.99 → 4999"""
    if amount is None:
        return 0
    return int(round(amount * MEDUSA_PRICE_FACTOR))


def create_medusa_log(request_type, status="Queued", medusa_id=None, request_data=None, response_data=None, error=None):
    """Create an Ecommerce Integration Log entry."""
    log = frappe.get_doc({
        "doctype": LOG_DOCTYPE,
        "integration": MODULE_NAME,
        "request_type": request_type,
        "status": status,
        "integration_item_code": medusa_id or "",
        "request_data": frappe.as_json(request_data) if request_data else "",
        "response_data": frappe.as_json(response_data) if response_data else "",
        "error": error or "",
    })
    log.insert(ignore_permissions=True)
    frappe.db.commit()
    return log.name


def update_medusa_log(log_name, **kwargs):
    """Update an existing Medusa log entry."""
    log = frappe.get_doc(LOG_DOCTYPE, log_name)
    for key, value in kwargs.items():
        if key in ("response_data", "request_data") and isinstance(value, dict):
            value = frappe.as_json(value)
        log.set(key, value)
    log.save(ignore_permissions=True)
    frappe.db.commit()
