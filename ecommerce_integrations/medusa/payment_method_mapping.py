"""Resolve a Medusa payment provider id to an ERPNext Mode of Payment.

Lookup order (mirrors the Shopware variant):
    1. ``Medusa Setting → payment_method_mappings`` (exact, then partial).
    2. Hardcoded :data:`MEDUSA_PAYMENT_METHOD_MAP` (exact, then partial).
    3. ``Medusa Setting.default_mode_of_payment``.
    4. :data:`DEFAULT_MODE_OF_PAYMENT`.

A resolved value is only returned when it actually exists as a ``Mode of
Payment`` on the site — otherwise ``None`` so the caller does not write a
broken Link.
"""

import frappe


DEFAULT_MODE_OF_PAYMENT = "Invoice"


# Built-in defaults for common Medusa payment providers. Keys are normalized
# provider_ids (lowercase, underscores). More specific keys come before
# generic ones so partial matching prefers the most specific entry.
MEDUSA_PAYMENT_METHOD_MAP = {
    # Stripe official plugin (provider ids vary by configured method)
    "pp_stripe_blik": "Stripe",
    "pp_stripe_giropay": "Stripe",
    "pp_stripe_ideal": "Stripe",
    "pp_stripe_bancontact": "Stripe",
    "pp_stripe_przelewy24": "Stripe",
    "pp_stripe_card": "Stripe",
    "pp_stripe_stripe": "Stripe",
    "pp_stripe": "Stripe",
    # PayPal community plugin
    "pp_paypal_paypal": "PayPal",
    "pp_paypal": "PayPal",
    # Klarna community plugin
    "pp_klarna_klarna": "Klarna",
    "pp_klarna": "Klarna",
}


def normalize_provider_id(provider_id: str) -> str:
    return (provider_id or "").strip().lower().replace("-", "_").replace(" ", "_")


def extract_provider_id(order: dict) -> str:
    """Return the most relevant payment provider id from a Medusa order.

    Looks at ``payments``, ``payment_sessions`` and ``payment_providers`` in
    that order across all payment_collections. Returns an empty string if no
    provider is recorded yet (e.g. unpaid order).
    """
    collections = (order or {}).get("payment_collections") or []
    for pc in collections:
        for payment in pc.get("payments") or []:
            pid = payment.get("provider_id")
            if pid:
                return pid
    for pc in collections:
        for session in pc.get("payment_sessions") or []:
            pid = session.get("provider_id")
            if pid:
                return pid
    for pc in collections:
        for provider in pc.get("payment_providers") or []:
            pid = provider.get("id")
            if pid:
                return pid
    return ""


def resolve_default_mode_of_payment(setting=None):
    if setting is not None:
        configured = getattr(setting, "default_mode_of_payment", None)
        if configured and frappe.db.exists("Mode of Payment", configured):
            return configured
    if frappe.db.exists("Mode of Payment", DEFAULT_MODE_OF_PAYMENT):
        return DEFAULT_MODE_OF_PAYMENT
    return None


def resolve_mode_of_payment(provider_id: str, setting=None):
    normalized = normalize_provider_id(provider_id)
    candidate = None

    rows = list(getattr(setting, "payment_method_mappings", None) or []) if setting else []

    if normalized:
        for row in rows:
            if normalize_provider_id(row.medusa_provider_id) == normalized:
                candidate = row.mode_of_payment
                break
        if not candidate:
            for row in rows:
                row_key = normalize_provider_id(row.medusa_provider_id)
                if row_key and (row_key in normalized or normalized in row_key):
                    candidate = row.mode_of_payment
                    break
        if not candidate and normalized in MEDUSA_PAYMENT_METHOD_MAP:
            candidate = MEDUSA_PAYMENT_METHOD_MAP[normalized]
        if not candidate:
            for key, value in MEDUSA_PAYMENT_METHOD_MAP.items():
                if key in normalized:
                    candidate = value
                    break

    if candidate and frappe.db.exists("Mode of Payment", candidate):
        return candidate

    return resolve_default_mode_of_payment(setting)
