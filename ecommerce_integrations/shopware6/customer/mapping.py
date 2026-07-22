"""
Shopware 6 Customer — field mappers.

Pure helpers that transform Shopware payloads (address, salutation) into
ERPNext field dicts, plus standalone factory functions for Address and
Contact that don't need the ``ShopwareCustomer`` class.
"""

from typing import Any

import frappe
from frappe import _

from ecommerce_integrations.shopware6.constants import ADDRESS_ID_FIELD
from ecommerce_integrations.shopware6.utils import (
    get_logger,
    map_country_code,
    map_state_from_shopware,
)


def _map_address_fields(
    shopware_address: dict[str, Any],
    customer_name: str,
    address_type: str,
    email: str | None = None,
    is_also_shipping: bool = False,
) -> dict[str, Any]:
    """Map Shopware address fields to ERPNext Address fields.

    Args:
        is_also_shipping: If True and address_type is Billing, also set is_shipping_address=1.
                          This is used when billing and shipping addresses are the same.
    """
    country_code = (
        shopware_address.get("country", {}).get("iso", "")
        if isinstance(shopware_address.get("country"), dict)
        else ""
    )
    country = map_country_code(country_code) or "Germany"

    state_data = shopware_address.get("countryState", {}) or {}
    state = map_state_from_shopware(state_data, country)

    # ERPNext automatically appends address_type to address_title when creating
    # the document name. Pass just the customer_name to avoid duplication like
    # "Customer-Billing-Billing".
    address_title = customer_name

    # is_primary_address = Preferred Billing Address
    # is_shipping_address = Preferred Shipping Address
    # When billing = shipping (is_also_shipping=True), set BOTH flags on billing.
    is_billing = address_type == "Billing"
    is_shipping = address_type == "Shipping" or (is_billing and is_also_shipping)

    street = shopware_address.get("street")
    city = shopware_address.get("city")
    if not street or not city:
        # Silently inventing placeholders ("Straße"/"Stadt") hides bad webhook
        # data and creates non-routable Address rows. Fail loudly instead.
        frappe.throw(
            _("Shopware-Adress-Payload fehlt Straße/Stadt; Adresse kann nicht angelegt werden.")
        )

    address_fields = {
        "address_title": address_title,
        "address_type": address_type,
        ADDRESS_ID_FIELD: shopware_address.get("id"),
        "address_line1": street,
        "address_line2": shopware_address.get("additionalAddressLine1", ""),
        "city": city,
        "state": state,
        "pincode": shopware_address.get("zipcode"),
        "country": country,
        "email_id": email,
        "is_primary_address": 1 if is_billing else 0,
        "is_shipping_address": 1 if is_shipping else 0,
    }

    phone = shopware_address.get("phoneNumber")
    if phone:
        address_fields["phone"] = phone

    return address_fields


def map_shopware_salutation(salutation_data: dict[str, Any]) -> tuple:
    """
    Map Shopware salutation to ERPNext salutation and gender.

    Shopware salutationKey can be:
    - English: mr, mrs, ms, not_specified (Shopware default)
    - German: herr, frau, nicht_angegeben (custom German shops)

    Returns:
        tuple: (salutation, gender)
    """
    if not salutation_data:
        return None, None

    salutation_key = (
        salutation_data.get("salutationKey", "")
        if isinstance(salutation_data, dict)
        else ""
    )
    display_name = (
        salutation_data.get("displayName", "")
        if isinstance(salutation_data, dict)
        else str(salutation_data)
    )

    gender_map = {
        "mr": "Male",
        "mrs": "Female",
        "ms": "Female",
        "not_specified": "Other",
        "herr": "Male",
        "frau": "Female",
        "nicht_angegeben": "Other",
        "divers": "Other",
    }

    salutation_map = {
        "mr": "Herr",
        "mrs": "Frau",
        "ms": "Frau",
        "not_specified": None,
        "herr": "Herr",
        "frau": "Frau",
        "nicht_angegeben": None,
        "divers": None,
    }

    key = salutation_key.lower() if salutation_key else ""
    gender = gender_map.get(key, None)
    salutation = salutation_map.get(key, display_name)

    return salutation, gender


def create_customer_contact(
    customer: str, customer_data: dict[str, Any], is_primary: bool = True
) -> str | None:
    """Create a Contact linked to ``customer``."""
    email = customer_data.get("email", "")
    first_name = customer_data.get("firstName", "")
    last_name = customer_data.get("lastName", "")
    salutation_data = customer_data.get("salutation", {})
    phone = customer_data.get("phone", "") or customer_data.get("phoneNumber", "")

    salutation, gender = map_shopware_salutation(salutation_data)

    contact = frappe.get_doc({
        "doctype": "Contact",
        "first_name": first_name or "Customer",
        "last_name": last_name,
        "salutation": salutation,
        "gender": gender,
        "is_primary_contact": 1 if is_primary else 0,
        "links": [{"link_doctype": "Customer", "link_name": customer}],
    })

    if email:
        contact.append("email_ids", {"email_id": email, "is_primary": 1})

    if phone:
        contact.append("phone_nos", {"phone": phone, "is_primary_phone": 1})

    try:
        contact.insert(ignore_permissions=True)
        return contact.name
    except Exception as e:
        logger = get_logger("create_contact")
        logger.error(f"Failed to create contact for {customer}", exception=e, persist=False)
        return None


def create_customer_address(
    customer: str,
    address_data: dict[str, Any],
    address_type: str = "Billing",
) -> str | None:
    """Create an Address linked to ``customer`` from raw Shopware address data."""
    if not address_data:
        return None

    street = address_data.get("street", "")
    additional = address_data.get("additionalAddressLine1", "")
    city = address_data.get("city", "")
    zipcode = address_data.get("zipcode", "")
    phone = address_data.get("phoneNumber", "")

    country = address_data.get("country", {})
    country_iso = country.get("iso", "") if isinstance(country, dict) else ""
    country_name = map_country_code(country_iso) or _("Germany")

    country_state = address_data.get("countryState", {})
    state_name = map_state_from_shopware(country_state, country_name)

    email = address_data.get("email", "")

    # ERPNext appends address_type to address_title, so pass just the
    # customer name to avoid "Customer-Billing-Billing".
    address_title = customer

    expected_name = f"{address_title}-{address_type}"
    if frappe.db.exists("Address", expected_name):
        return expected_name

    address_doc: dict[str, Any] = {
        "doctype": "Address",
        "address_title": address_title,
        "address_type": address_type,
        "address_line1": street,
        "address_line2": additional,
        "city": city,
        "pincode": zipcode,
        "country": country_name,
        "links": [{"link_doctype": "Customer", "link_name": customer}],
        "is_primary_address": 1 if address_type == "Billing" else 0,
        "is_shipping_address": 1 if address_type == "Shipping" else 0,
    }

    if state_name:
        address_doc["state"] = state_name
    if phone:
        address_doc["phone"] = phone
    if email:
        address_doc["email_id"] = email

    address = frappe.get_doc(address_doc)
    try:
        address.insert(ignore_permissions=True)
        return address.name
    except Exception as e:
        logger = get_logger("create_customer_address")
        logger.error(f"Failed to create address for {customer}", exception=e, persist=False)
        return None
