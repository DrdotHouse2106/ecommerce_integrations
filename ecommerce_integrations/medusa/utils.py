"""Shared utilities for the Medusa integration."""

import frappe
from frappe.utils import cstr, get_datetime, now

from ecommerce_integrations.medusa.constants import (
    CATEGORY_MODE_SMART_COLLECTIONS_ONLY,
    LOG_DOCTYPE,
    MEDUSA_PRICE_FACTOR,
    MODULE_NAME,
    SETTING_DOCTYPE,
)
from ecommerce_integrations.medusa.services.log_sanitizer import (
    sanitize_medusa_log_data,
    sanitize_medusa_log_text,
)


def _serialize_log_blob(value):
    """Sanitize and JSON-serialize a request/response payload for log storage.

    Accepts dicts/lists/strings/None and returns a JSON string suitable for
    the ``request_data`` / ``response_data`` text columns. Secrets are
    redacted by ``sanitize_medusa_log_data`` before serialization so they
    never reach the database.
    """
    if value in (None, ""):
        return ""
    sanitized = sanitize_medusa_log_data(value)
    if isinstance(sanitized, str):
        return sanitized
    return frappe.as_json(sanitized)


def _serialize_log_text(value):
    """Sanitize a free-form text field (e.g. traceback) for log storage."""
    if not value:
        return ""
    if isinstance(value, str):
        return sanitize_medusa_log_text(value)
    return sanitize_medusa_log_text(str(value))


def is_medusa_enabled() -> bool:
    """Check if Medusa integration is enabled."""
    try:
        return bool(frappe.db.get_single_value(SETTING_DOCTYPE, "enable_medusa"))
    except Exception:
        return False


def get_medusa_setting():
    """Get cached Medusa Setting document."""
    return frappe.get_cached_doc(SETTING_DOCTYPE)


def is_smart_collections_only(setting=None) -> bool:
    """True when Item-Group → Medusa-category mirroring is disabled.

    Centralises the mode-string check so the literal lives in one place.
    """
    if setting is None:
        setting = get_medusa_setting()
    return setting.category_assignment_mode == CATEGORY_MODE_SMART_COLLECTIONS_ONLY


def medusa_price_to_erpnext(amount: float) -> float:
    """Convert Medusa price to ERPNext. With PRICE_FACTOR=1 this is a no-op."""
    if amount is None:
        return 0.0
    return round(amount / MEDUSA_PRICE_FACTOR, 2)


def erpnext_price_to_medusa(amount: float) -> float:
    """Convert ERPNext price to Medusa decimal amount. 49.99 → 49.99"""
    if amount is None:
        return 0.0
    return round(amount * MEDUSA_PRICE_FACTOR, 2)


def create_medusa_log(request_type, status="Queued", medusa_id=None, request_data=None, response_data=None, error=None):
    """Create an Ecommerce Integration Log entry.

    Sensitive keys (tokens, secrets, the Medusa ``publishable_api_key``) are
    redacted from ``request_data`` / ``response_data`` / the traceback before
    they hit the database — see ``services/log_sanitizer.py``.
    """
    log = frappe.get_doc({
        "doctype": LOG_DOCTYPE,
        "integration": MODULE_NAME,
        "method": request_type,
        "status": status,
        "message": medusa_id or "",
        "request_data": _serialize_log_blob(request_data),
        "response_data": _serialize_log_blob(response_data),
        "traceback": _serialize_log_text(error),
    })
    log.insert(ignore_permissions=True)
    frappe.db.commit()
    return log.name


# ─── Ecommerce Item helpers (canonical Medusa ↔ ERPNext mapping) ─────
#
# Medusa stores its external IDs in ``tabEcommerce Item`` with
# ``integration='medusa'``, just like Shopware. The helpers below are
# the read/write API every Medusa module should use — never read
# ``tabItem.medusa_product_id`` directly anymore (the column is dropped
# by ``patches/drop_medusa_item_custom_fields.py``).


def get_medusa_product_id(item_code: str) -> str | None:
    """Return the Medusa product id for an ERPNext item, or None."""
    if not item_code:
        return None
    return frappe.db.get_value(
        "Ecommerce Item",
        {"integration": MODULE_NAME, "erpnext_item_code": item_code},
        "integration_item_code",
    ) or None


def get_medusa_variant_id(item_code: str) -> str | None:
    """Return the Medusa variant id for an ERPNext item, or None."""
    if not item_code:
        return None
    return frappe.db.get_value(
        "Ecommerce Item",
        {"integration": MODULE_NAME, "erpnext_item_code": item_code},
        "variant_id",
    ) or None


def get_medusa_mapping(item_code: str) -> dict | None:
    """Return the full Medusa mapping row for an ERPNext item, or None.

    Returns a dict with keys ``integration_item_code`` (product id),
    ``variant_id``, ``has_variants``, ``variant_of`` — matching the
    ``tabEcommerce Item`` schema.
    """
    if not item_code:
        return None
    return frappe.db.get_value(
        "Ecommerce Item",
        {"integration": MODULE_NAME, "erpnext_item_code": item_code},
        ["name", "integration_item_code", "variant_id", "has_variants", "variant_of"],
        as_dict=True,
    )


def is_synced_to_medusa(item_code: str) -> bool:
    """True when the item has a Medusa product id mapped."""
    return bool(get_medusa_product_id(item_code))


def upsert_medusa_mapping(
    erpnext_item_code: str,
    integration_item_code: str,
    *,
    variant_id: str | None = None,
    has_variants: int = 0,
    variant_of: str | None = None,
) -> None:
    """Create or update the ``tabEcommerce Item`` row for a Medusa product.

    Safe to call repeatedly: the existing row (matched by
    ``integration='medusa'`` + ``erpnext_item_code``) is updated in
    place, and a fresh row is inserted otherwise.
    """
    if not erpnext_item_code or not integration_item_code:
        return

    existing = frappe.db.get_value(
        "Ecommerce Item",
        {"integration": MODULE_NAME, "erpnext_item_code": erpnext_item_code},
        "name",
    )
    sku = None if has_variants else erpnext_item_code
    if existing:
        # Update without going through the controller — the
        # controller's ``before_insert`` runs unique-constraint checks
        # that would trip on re-syncs.
        frappe.db.set_value(
            "Ecommerce Item",
            existing,
            {
                "integration_item_code": integration_item_code,
                "variant_id": cstr(variant_id) if variant_id else None,
                "has_variants": int(bool(has_variants)),
                "variant_of": cstr(variant_of) if variant_of else None,
                "sku": sku,
                "item_synced_on": now(),
            },
            update_modified=True,
        )
        return

    doc = frappe.get_doc({
        "doctype": "Ecommerce Item",
        "integration": MODULE_NAME,
        "erpnext_item_code": erpnext_item_code,
        "integration_item_code": integration_item_code,
        "variant_id": cstr(variant_id) if variant_id else None,
        "has_variants": int(bool(has_variants)),
        "variant_of": cstr(variant_of) if variant_of else None,
        "sku": sku,
        "inventory_synced_on": get_datetime("1970-01-01"),
        "item_synced_on": now(),
    })
    doc.flags.from_integration = True
    doc.insert(ignore_permissions=True)


def clear_medusa_mapping(erpnext_item_code: str) -> None:
    """Delete the ``tabEcommerce Item`` row for a Medusa product, if any."""
    name = frappe.db.get_value(
        "Ecommerce Item",
        {"integration": MODULE_NAME, "erpnext_item_code": erpnext_item_code},
        "name",
    )
    if name:
        frappe.delete_doc("Ecommerce Item", name, ignore_permissions=True, force=True)


def clear_medusa_mapping_by_product_id(integration_item_code: str) -> None:
    """Delete the ``tabEcommerce Item`` row(s) for a stale Medusa product id."""
    names = frappe.get_all(
        "Ecommerce Item",
        filters={"integration": MODULE_NAME, "integration_item_code": integration_item_code},
        pluck="name",
    )
    for name in names:
        frappe.delete_doc("Ecommerce Item", name, ignore_permissions=True, force=True)


def update_medusa_log(log_name, **kwargs):
    """Update an existing Medusa log entry.

    Request/response payloads and traceback strings are sanitized through
    the log sanitizer before persistence — same contract as
    ``create_medusa_log``.
    """
    # Map caller-friendly names to actual doctype field names
    if "error" in kwargs:
        kwargs["traceback"] = kwargs.pop("error")
    log = frappe.get_doc(LOG_DOCTYPE, log_name)
    for key, value in kwargs.items():
        if key in ("response_data", "request_data"):
            value = _serialize_log_blob(value)
        elif key == "traceback":
            value = _serialize_log_text(value)
        log.set(key, value)
    log.save(ignore_permissions=True)
    frappe.db.commit()


def log_error(title: str, message) -> None:
    """Thin wrapper over ``frappe.log_error`` with safer defaults.

    - Auto-detects Exception objects: passes ``frappe.get_traceback()`` as
      the message so the full traceback is recorded, not just ``str(e)``.
    - Truncates the title to 140 characters (the doctype's column limit) so
      a long title doesn't itself raise.
    - Argument order is ``(title, message)`` — the opposite of the
      historical ``frappe.log_error(message, title)`` order that callers
      kept getting backwards.
    """
    if isinstance(message, BaseException):
        body = frappe.get_traceback()
    else:
        body = str(message) if message is not None else ""
    safe_title = (title or "")[:140]
    frappe.log_error(message=body, title=safe_title)
