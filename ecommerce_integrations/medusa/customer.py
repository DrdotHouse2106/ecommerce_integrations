"""Bidirectional customer sync between Medusa v2 and ERPNext."""

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
        """Fetch full customer data from Medusa API."""
        return medusa_request(session, base_url, "GET", f"{API_CUSTOMERS}/{self.medusa_id}")

    def sync_to_erpnext(self, medusa_data: dict = None):
        """Create or update ERPNext Customer from Medusa data."""
        if medusa_data is None:
            result = self.fetch_from_medusa()
            medusa_data = result.get("customer", {})

        customer_name = self.get_existing_customer()

        if customer_name:
            self._update_customer(customer_name, medusa_data)
        else:
            customer_name = self._create_customer(medusa_data)

        return customer_name

    def _create_customer(self, data: dict) -> str:
        """Create a new ERPNext Customer from Medusa data."""
        company_name = data.get("company_name", "")
        first_name = data.get("first_name", "")
        last_name = data.get("last_name", "")
        email = data.get("email", "")

        if company_name:
            cust_name = company_name
            customer_type = "Company"
        elif first_name or last_name:
            cust_name = f"{first_name} {last_name}".strip()
            customer_type = "Individual"
        else:
            cust_name = email
            customer_type = "Individual"

        customer = frappe.get_doc({
            "doctype": "Customer",
            "customer_name": cust_name,
            "customer_type": customer_type,
            "customer_group": self.setting.customer_group or "All Customer Groups",
            CUSTOMER_ID_FIELD: self.medusa_id,
        })
        customer.flags.ignore_mandatory = True
        customer.insert(ignore_permissions=True)

        if email or data.get("phone"):
            self._create_contact(customer.name, data)

        for addr in data.get("addresses", []):
            self._create_address(customer.name, addr)

        frappe.db.commit()
        return customer.name

    def _update_customer(self, customer_name: str, data: dict):
        """Update existing ERPNext Customer with Medusa data."""
        customer = frappe.get_doc("Customer", customer_name)
        company_name = data.get("company_name", "")

        if company_name and company_name != customer.customer_name:
            customer.customer_name = company_name
        elif not company_name:
            name = f"{data.get('first_name', '')} {data.get('last_name', '')}".strip()
            if name and name != customer.customer_name:
                customer.customer_name = name

        customer.save(ignore_permissions=True)
        frappe.db.commit()

    def _create_contact(self, customer_name: str, data: dict):
        """Create a Contact linked to the customer."""
        contact = frappe.get_doc({
            "doctype": "Contact",
            "first_name": data.get("first_name", ""),
            "last_name": data.get("last_name", ""),
        })

        if data.get("email"):
            contact.append("email_ids", {"email_id": data["email"], "is_primary": 1})
        if data.get("phone"):
            contact.append("phone_nos", {"phone": data["phone"], "is_primary_phone": 1})

        contact.append("links", {
            "link_doctype": "Customer",
            "link_name": customer_name,
        })
        contact.flags.ignore_mandatory = True
        contact.insert(ignore_permissions=True)

    def _create_address(self, customer_name: str, addr: dict):
        """Create an Address linked to the customer."""
        address = frappe.get_doc({
            "doctype": "Address",
            "address_title": customer_name,
            "address_type": "Billing",
            "address_line1": addr.get("address_1", ""),
            "address_line2": addr.get("address_2", ""),
            "city": addr.get("city", ""),
            "state": addr.get("province", ""),
            "pincode": addr.get("postal_code", ""),
            "country": _get_country_name(addr.get("country_code", "DE")),
            "phone": addr.get("phone", ""),
        })
        address.append("links", {
            "link_doctype": "Customer",
            "link_name": customer_name,
        })
        address.flags.ignore_mandatory = True
        address.insert(ignore_permissions=True)


def _get_country_name(country_code: str) -> str:
    """Convert ISO country code to ERPNext country name."""
    if not country_code:
        return "Germany"
    name = frappe.db.get_value("Country", {"code": country_code.lower()})
    return name or "Germany"


def sync_customer_by_id(entity_id: str, event_type: str = ""):
    """Entry point for webhook-triggered customer sync."""
    if not is_medusa_enabled():
        return

    log_name = create_medusa_log(
        request_type=f"Customer Sync ({event_type})",
        medusa_id=entity_id,
        status="In Progress",
    )

    try:
        customer = MedusaCustomer(entity_id)
        customer_name = customer.sync_to_erpnext()
        update_medusa_log(log_name, status="Success", response_data={"customer": customer_name})
    except Exception as e:
        update_medusa_log(log_name, status="Error", error=str(e))
        frappe.log_error(f"Medusa customer sync failed: {entity_id}", str(e))
