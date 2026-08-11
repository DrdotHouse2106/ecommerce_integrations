"""Resolve Shopware customer-group / payment-method / salutation defaults
by operator-typed display name, for the ERP-to-Shopware customer push
(``push.py``).

Deliberately resolve-or-throw, not resolve-or-create (unlike
``get_or_create_manufacturer``/``get_or_create_unit`` in
``export/product_mapper.py``): silently creating a pricing-relevant
Shopware Customer Group would be unsafe — a loud config error is the right
failure mode here.
"""

import frappe
from frappe import _

from ecommerce_integrations.shopware6.base.cache_manager import get_cache


def resolve_shopware_customer_group_id(client, group_name: str) -> str:
    cache = get_cache()
    cached = cache.get("customer_group", group_name)
    if cached:
        return cached

    response = client.request_post(
        "search/customer-group",
        {"filter": [{"type": "equals", "field": "name", "value": group_name}], "limit": 1},
    )
    groups = response.data or []
    if not groups:
        frappe.throw(
            _("Shopware-Kundengruppe '{0}' nicht gefunden. Bitte in den Shopware-Einstellungen "
              "korrigieren.").format(group_name)
        )
    group_id = groups[0]["id"]
    cache.set("customer_group", group_name, group_id)
    return group_id


def resolve_shopware_payment_method_id(client, method_name: str) -> str:
    cache = get_cache()
    cached = cache.get("payment_method", method_name)
    if cached:
        return cached

    response = client.request_post(
        "search/payment-method",
        {"filter": [{"type": "equals", "field": "name", "value": method_name}], "limit": 1},
    )
    methods = response.data or []
    if not methods:
        frappe.throw(
            _("Shopware-Zahlungsart '{0}' nicht gefunden. Bitte in den Shopware-Einstellungen "
              "korrigieren.").format(method_name)
        )
    method_id = methods[0]["id"]
    cache.set("payment_method", method_name, method_id)
    return method_id


def resolve_shopware_salutation_id(client, salutation_key: str) -> str:
    cache = get_cache()
    cached = cache.get("salutation", salutation_key)
    if cached:
        return cached

    response = client.request_post(
        "search/salutation",
        {"filter": [{"type": "equals", "field": "salutationKey", "value": salutation_key}], "limit": 1},
    )
    salutations = response.data or []
    if not salutations:
        frappe.throw(
            _("Shopware-Anrede mit Schlüssel '{0}' nicht gefunden. Bitte in den "
              "Shopware-Einstellungen korrigieren.").format(salutation_key)
        )
    salutation_id = salutations[0]["id"]
    cache.set("salutation", salutation_key, salutation_id)
    return salutation_id
