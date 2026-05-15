"""Bidirectional customer sync between Medusa v2 and ERPNext.

Customer lookup cascade (used to dedupe across guest/registered checkouts):
  1. Customer with matching ``medusa_customer_id``.
  2. Contact whose primary email matches Medusa ``email`` and is linked to a
     Customer (only when the Medusa customer has no account, ``has_account``
     False — registered customers are authoritative on their own id).
  3. Otherwise create a new Customer.
"""

import frappe

from ecommerce_integrations.medusa.connection import medusa_request, temp_medusa_session
from ecommerce_integrations.medusa.constants import (
    API_CUSTOMERS,
    CUSTOMER_ID_FIELD,
    SETTING_DOCTYPE,
)
from ecommerce_integrations.medusa.utils import create_medusa_log, is_medusa_enabled, update_medusa_log


class MedusaCustomer:
    """Handles syncing a single Medusa customer to/from ERPNext."""

    def __init__(self, medusa_customer_id: str):
        self.medusa_id = medusa_customer_id
        self.setting = frappe.get_cached_doc(SETTING_DOCTYPE)

    def get_existing_customer(self):
        """Find existing ERPNext Customer linked to this Medusa ID."""
        return frappe.db.get_value("Customer", {CUSTOMER_ID_FIELD: self.medusa_id})

    def is_synced(self) -> bool:
        return bool(self.get_existing_customer())

    @temp_medusa_session
    def fetch_from_medusa(self, session, base_url) -> dict:
        """Fetch full customer data from Medusa API (fallback when payload not enriched)."""
        return medusa_request(session, base_url, "GET", f"{API_CUSTOMERS}/{self.medusa_id}")

    def sync_to_erpnext(self, medusa_data: dict | None = None):
        """Create or update ERPNext Customer from Medusa data.

        ``medusa_data`` should be the customer object itself (id, email, addresses, ...).
        If omitted, the data is fetched via the Medusa Admin API.
        """
        if medusa_data is None:
            result = self.fetch_from_medusa()
            medusa_data = result.get("customer", {})

        customer_name = self.get_existing_customer()
        if not customer_name:
            customer_name = self._find_by_email_contact(medusa_data)
            if customer_name:
                frappe.db.set_value(
                    "Customer", customer_name, CUSTOMER_ID_FIELD, self.medusa_id,
                    update_modified=False,
                )

        if customer_name:
            self._update_customer(customer_name, medusa_data)
        else:
            customer_name = self._create_customer(medusa_data)

        return customer_name

    def _find_by_email_contact(self, data: dict) -> str | None:
        """Find a Customer via a Contact whose primary email matches.

        Only used when the Medusa customer has no account, to merge guest
        checkouts placed under the same email.
        """
        if data.get("has_account"):
            return None
        email = (data.get("email") or "").strip().lower()
        if not email:
            return None
        rows = frappe.db.sql(
            """
            SELECT dl.link_name
            FROM `tabContact Email` ce
            JOIN `tabContact` c ON c.name = ce.parent
            JOIN `tabDynamic Link` dl
              ON dl.parent = c.name
             AND dl.parenttype = 'Contact'
             AND dl.link_doctype = 'Customer'
            WHERE LOWER(ce.email_id) = %s
            ORDER BY ce.is_primary DESC, c.creation ASC
            LIMIT 1
            """,
            (email,),
        )
        return rows[0][0] if rows else None

    def _create_customer(self, data: dict) -> str:
        """Create a new ERPNext Customer from Medusa data."""
        company_name = (data.get("company_name") or "").strip()
        first_name = (data.get("first_name") or "").strip()
        last_name = (data.get("last_name") or "").strip()
        email = (data.get("email") or "").strip()

        if company_name:
            cust_name = company_name
            customer_type = "Company"
        elif first_name or last_name:
            cust_name = f"{first_name} {last_name}".strip()
            customer_type = "Individual"
        else:
            cust_name = email or self.medusa_id
            customer_type = "Individual"

        metadata = data.get("metadata") or {}

        customer = frappe.get_doc({
            "doctype": "Customer",
            "customer_name": cust_name,
            "customer_type": customer_type,
            "customer_group": self.setting.customer_group or "All Customer Groups",
            "tax_id": metadata.get("vat_id", ""),
            "buyer_reference": metadata.get("leitweg_id", ""),
            CUSTOMER_ID_FIELD: self.medusa_id,
        })
        customer.flags.ignore_mandatory = True
        customer.insert(ignore_permissions=True)

        if email or data.get("phone"):
            self._create_contact(customer.name, data, is_primary=True)

        default_billing_id = data.get("default_billing_address_id")
        default_shipping_id = data.get("default_shipping_address_id")
        for addr in data.get("addresses", []) or []:
            is_default_billing = bool(
                addr.get("is_default_billing")
                or (default_billing_id and addr.get("id") == default_billing_id)
            )
            is_default_shipping = bool(
                addr.get("is_default_shipping")
                or (default_shipping_id and addr.get("id") == default_shipping_id)
            )
            self._create_address(
                customer.name, addr,
                is_primary_billing=is_default_billing,
                is_primary_shipping=is_default_shipping,
                fallback_email=email,
            )

        frappe.db.commit()
        return customer.name

    def _update_customer(self, customer_name: str, data: dict):
        """Update existing ERPNext Customer with Medusa data."""
        customer = frappe.get_doc("Customer", customer_name)
        company_name = (data.get("company_name") or "").strip()

        if company_name and company_name != customer.customer_name:
            customer.customer_name = company_name
        elif not company_name:
            full_name = f"{data.get('first_name', '')} {data.get('last_name', '')}".strip()
            if full_name and full_name != customer.customer_name:
                customer.customer_name = full_name

        metadata = data.get("metadata") or {}
        vat_id = metadata.get("vat_id", "")
        if vat_id and vat_id != customer.tax_id:
            customer.tax_id = vat_id
        leitweg_id = metadata.get("leitweg_id", "")
        if leitweg_id and leitweg_id != customer.buyer_reference:
            customer.buyer_reference = leitweg_id

        customer.save(ignore_permissions=True)
        frappe.db.commit()

    def _create_contact(self, customer_name: str, data: dict, is_primary: bool = False):
        """Create a Contact linked to the customer (no-op if email/phone unchanged)."""
        email = (data.get("email") or "").strip()
        phone = (data.get("phone") or "").strip()

        if email:
            existing = frappe.db.sql(
                """
                SELECT c.name
                FROM `tabContact` c
                JOIN `tabContact Email` ce ON ce.parent = c.name
                JOIN `tabDynamic Link` dl
                  ON dl.parent = c.name
                 AND dl.parenttype = 'Contact'
                 AND dl.link_doctype = 'Customer'
                 AND dl.link_name = %s
                WHERE LOWER(ce.email_id) = %s
                LIMIT 1
                """,
                (customer_name, email.lower()),
            )
            if existing:
                return existing[0][0]

        contact = frappe.get_doc({
            "doctype": "Contact",
            "first_name": data.get("first_name", "") or (email.split("@")[0] if email else customer_name),
            "last_name": data.get("last_name", ""),
        })
        if email:
            contact.append("email_ids", {"email_id": email, "is_primary": 1})
        if phone:
            contact.append("phone_nos", {"phone": phone, "is_primary_phone": 1, "is_primary_mobile_no": 1})
        contact.append("links", {
            "link_doctype": "Customer",
            "link_name": customer_name,
        })
        contact.flags.ignore_mandatory = True
        contact.insert(ignore_permissions=True)

        if is_primary:
            updates = {}
            if email:
                updates["customer_primary_contact"] = contact.name
                updates["email_id"] = email
            if phone:
                updates["mobile_no"] = phone
            if updates:
                frappe.db.set_value("Customer", customer_name, updates, update_modified=False)
        return contact.name

    def _create_address(
        self,
        customer_name: str,
        addr: dict,
        is_primary_billing: bool = False,
        is_primary_shipping: bool = False,
        fallback_email: str = "",
    ):
        """Create an Address linked to the customer."""
        address_type = "Billing" if is_primary_billing else ("Shipping" if is_primary_shipping else "Other")
        title_full_name = f"{addr.get('first_name', '')} {addr.get('last_name', '')}".strip()
        address = frappe.get_doc({
            "doctype": "Address",
            "address_title": title_full_name or customer_name,
            "address_type": address_type,
            "address_line1": addr.get("address_1", ""),
            "address_line2": addr.get("address_2", ""),
            "city": addr.get("city", ""),
            "state": addr.get("province", ""),
            "pincode": addr.get("postal_code", ""),
            "country": _get_country_name(addr.get("country_code", "")),
            "phone": addr.get("phone", ""),
            "email_id": fallback_email or "",
            "is_primary_address": 1 if is_primary_billing else 0,
            "is_shipping_address": 1 if is_primary_shipping else 0,
        })
        address.append("links", {
            "link_doctype": "Customer",
            "link_name": customer_name,
        })
        address.flags.ignore_mandatory = True
        address.insert(ignore_permissions=True)
        return address.name


def _get_country_name(country_code: str) -> str:
    """Convert ISO country code to ERPNext country name."""
    if not country_code:
        return _get_default_country()
    name = frappe.db.get_value("Country", {"code": country_code.lower()})
    return name or _get_default_country()


def _get_default_country() -> str:
    """Get the default country from Medusa Setting, falling back to Company default."""
    setting = frappe.get_cached_doc("Medusa Setting")
    if setting.default_country:
        return setting.default_country
    if setting.company:
        return frappe.db.get_value("Company", setting.company, "country") or frappe.db.get_default("country") or "Germany"
    return frappe.db.get_default("country") or "Germany"


def _extract_customer(payload) -> dict | None:
    """Pull the customer dict out of a webhook payload, if present."""
    if isinstance(payload, dict):
        cust = payload.get("customer")
        if isinstance(cust, dict) and cust.get("id"):
            return cust
    return None


def sync_customer_by_id(entity_id: str | None = None, payload: dict | None = None, event_type: str = ""):
    """Entry point for webhook-triggered customer sync.

    Accepts either a payload (enriched form) or a bare entity_id (legacy form
    and scheduled-sync callers). When the payload contains a ``customer``
    object we use it directly; otherwise we fetch via the Admin API.
    """
    if not is_medusa_enabled():
        return

    customer_data = _extract_customer(payload)
    medusa_id = (customer_data or {}).get("id") or entity_id
    if not medusa_id:
        return

    log_name = create_medusa_log(
        request_type=f"Customer Sync ({event_type})",
        medusa_id=medusa_id,
        status="In Progress",
    )

    try:
        customer = MedusaCustomer(medusa_id)
        customer_name = customer.sync_to_erpnext(customer_data)
        update_medusa_log(log_name, status="Success", response_data={"customer": customer_name})
    except Exception as e:
        update_medusa_log(log_name, status="Error", error=str(e))
        frappe.log_error(f"Medusa customer sync failed: {medusa_id}", str(e))


def handle_customer_deleted(entity_id: str | None = None, payload: dict | None = None, event_type: str = ""):
    """Disable the ERPNext customer mirror when the Medusa customer is deleted.

    We do not actually delete the Customer doctype because it may be referenced
    by Sales Orders, Invoices, etc. Instead we clear the medusa_customer_id
    (so future events don't match the wrong record) and set ``disabled = 1``.
    """
    if not is_medusa_enabled():
        return

    medusa_id = entity_id or (payload or {}).get("id")
    if not medusa_id:
        return

    log_name = create_medusa_log(
        request_type=f"Customer Sync ({event_type})",
        medusa_id=medusa_id,
        status="In Progress",
    )
    try:
        customer_name = frappe.db.get_value("Customer", {CUSTOMER_ID_FIELD: medusa_id})
        if customer_name:
            frappe.db.set_value(
                "Customer", customer_name,
                {CUSTOMER_ID_FIELD: "", "disabled": 1},
                update_modified=False,
            )
            frappe.db.commit()
            update_medusa_log(log_name, status="Success", response_data={"customer": customer_name, "disabled": True})
        else:
            update_medusa_log(log_name, status="Skipped", response_data={"message": "No matching customer"})
    except Exception as e:
        update_medusa_log(log_name, status="Error", error=str(e))
        frappe.log_error(f"Medusa customer delete failed: {medusa_id}", str(e))
