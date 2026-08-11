"""ERPNext → Shopware customer push (opt-in, creation-only).

Most ERPNext customers are wholesale/internal and never meant to appear in
the storefront, so this only fires for customers with the "Nach Shopware
übertragen" checkbox ticked (``Customer.push_to_shopware``) AND the
Setting-level master switch (``Shopware Setting.enable_customer_push``) on.

Creates a real (non-guest) Shopware customer with no password set — not a
guest account. Guest accounts are an internal Shopware checkout
implementation detail, not designed to be upgraded into a login-capable
account later via the storefront's "Passwort vergessen" flow; a real
account with no password sidesteps that entirely; the customer activates
login later through the normal reset-mail flow.

Creation-only: once ``shopware_customer_id`` is set, later edits to the
Customer doc are not propagated back out (see ``vat.py`` for the separate,
unconditional number-unification path, which *is* self-healing on every
inbound Shopware webhook).
"""

import frappe
from frappe import _

from ecommerce_integrations.shopware6.connection import temp_shopware_session
from ecommerce_integrations.shopware6.constants import CUSTOMER_ID_FIELD, SETTING_DOCTYPE

from .group_resolver import (
    resolve_shopware_customer_group_id,
    resolve_shopware_payment_method_id,
    resolve_shopware_salutation_id,
)


def queue_customer_for_sync(doc, method=None):
    """``Customer`` after_insert/on_update. Mirrors
    ``services/queue_hooks.py::queue_item_for_sync``'s gate shape."""
    if doc.flags.from_integration:
        return
    if getattr(frappe.flags, "skip_shopware_sync", False):
        return
    if not getattr(doc, "push_to_shopware", False):
        return
    if getattr(doc, CUSTOMER_ID_FIELD, None):
        return  # already pushed — creation-only, no ongoing field sync

    setting = _get_enabled_setting_with_customer_push()
    if not setting:
        return

    frappe.enqueue(
        "ecommerce_integrations.shopware6.customer.push.push_customer_to_shopware",
        queue="short",
        customer_name=doc.name,
        enqueue_after_commit=True,
        job_id=f"shopware_customer_push:{doc.name}",
        deduplicate=True,
    )


@temp_shopware_session
def push_customer_to_shopware(client, customer_name: str) -> None:
    customer = frappe.get_doc("Customer", customer_name)
    if getattr(customer, CUSTOMER_ID_FIELD, None):
        return  # already pushed by a racing/duplicate job

    setting = frappe.get_cached_doc(SETTING_DOCTYPE)
    payload = _build_customer_payload(client, customer, setting)

    response = client.request_post("customer", payload)
    shopware_id = (response.data or {}).get("id") if response.data else None
    shopware_id = shopware_id or payload["id"]

    frappe.db.set_value("Customer", customer_name, CUSTOMER_ID_FIELD, shopware_id)
    frappe.db.commit()  # narrow the race window against Shopware's own
    # customer.written webhook bouncing straight back before this commits


def _get_enabled_setting_with_customer_push():
    try:
        setting = frappe.get_cached_doc(SETTING_DOCTYPE)
    except Exception:
        return None
    if not setting.is_enabled():
        return None
    if not getattr(setting, "enable_customer_push", False):
        return None
    return setting


def _build_customer_payload(client, customer, setting) -> dict:
    default_sales_channel = next(
        (sc for sc in (setting.sales_channels or []) if getattr(sc, "is_default", False)), None
    )
    if not default_sales_channel or not default_sales_channel.sales_channel_id:
        frappe.throw(
            _("Kein Shopware-Sales-Channel als Standard markiert (\"Ist Standard\") — wird für "
              "die Übertragung neuer Kunden benötigt.")
        )

    first_name, last_name = _resolve_contact_name(customer)
    email = customer.email_id or f"{customer.name}@placeholder.invalid"

    payload = {
        "id": frappe.generate_hash(length=32),
        "customerNumber": customer.name,
        "email": email,
        "firstName": first_name,
        "lastName": last_name,
        "groupId": resolve_shopware_customer_group_id(
            client, setting.shopware_default_customer_group_name
        ),
        "salesChannelId": default_sales_channel.sales_channel_id,
        "salutationId": resolve_shopware_salutation_id(
            client, setting.shopware_default_salutation_key or "not_specified"
        ),
        "defaultPaymentMethodId": resolve_shopware_payment_method_id(
            client, setting.shopware_default_payment_method_name
        ),
    }
    if customer.customer_type == "Company":
        payload["company"] = customer.customer_name
    return payload


def _resolve_contact_name(customer) -> tuple[str, str]:
    contact = frappe.db.sql(
        """
        SELECT c.first_name, c.last_name
        FROM `tabContact` c
        INNER JOIN `tabDynamic Link` dl ON dl.parent = c.name AND dl.parenttype = 'Contact'
        WHERE dl.link_doctype = 'Customer' AND dl.link_name = %s
        ORDER BY c.is_primary_contact DESC
        LIMIT 1
        """,
        (customer.name,),
        as_dict=True,
    )
    if contact and (contact[0].first_name or contact[0].last_name):
        return contact[0].first_name or "-", contact[0].last_name or "-"
    return customer.customer_name or customer.name, "-"
