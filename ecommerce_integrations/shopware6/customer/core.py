"""
Shopware 6 Customer — core class and sync lock.

The ``ShopwareCustomer`` class extends ``EcommerceCustomer`` and owns the
customer doc / contact / address / billing-contact lifecycle. Sync entry
points and helper mappers live in sibling modules and call into this class.
"""

from contextlib import contextmanager
from time import perf_counter
from typing import Any

import frappe
from frappe import _

from ecommerce_integrations.controllers.customer import EcommerceCustomer, resolve_customer_type
from ecommerce_integrations.shopware6.constants import (
    ADDRESS_ID_FIELD,
    CUSTOMER_ID_FIELD,
    MODULE_NAME,
    SETTING_DOCTYPE,
)
from ecommerce_integrations.shopware6.utils import get_logger

from .mapping import _map_address_fields, map_shopware_salutation
from .vat import trigger_vat_id_check

CUSTOMER_SYNC_LOCK_TIMEOUT_SECONDS = 10 * 60
CUSTOMER_SYNC_LOCK_PREFIX = "shopware6_customer_sync"


class CustomerSyncInProgressError(frappe.ValidationError):
    """Raised when another worker is already syncing the same Shopware customer."""


@contextmanager
def _acquire_customer_sync_lock(customer_id: str):
    """Serialize sync operations per Shopware customer ID."""
    lock = frappe.cache().lock(
        f"{CUSTOMER_SYNC_LOCK_PREFIX}_{customer_id}",
        timeout=CUSTOMER_SYNC_LOCK_TIMEOUT_SECONDS,
    )
    if not lock.acquire(blocking=False):
        raise CustomerSyncInProgressError(
            _("Kunden-Sync läuft bereits für Shopware-Kunde {0}").format(customer_id)
        )

    try:
        yield
    finally:
        try:
            lock.release()
        except Exception:
            # Lock timeout is the fallback in case release fails.
            pass


class ShopwareCustomer(EcommerceCustomer):
    """
    Shopware Customer handler following the same pattern as ShopifyCustomer.

    Uses the EcommerceCustomer base class for consistent customer handling
    across all integrations.
    """

    def __init__(self, customer_id: str):
        self.setting = frappe.get_doc(SETTING_DOCTYPE)
        super().__init__(customer_id, CUSTOMER_ID_FIELD, MODULE_NAME)

    def sync_customer(self, customer: dict[str, Any], sales_channel_id: str | None = None) -> None:
        """Create Customer in ERPNext using Shopware's Customer dict."""

        first_name = customer.get("firstName", "") or ""
        last_name = customer.get("lastName", "") or ""
        company = customer.get("company", "") or ""
        email = customer.get("email", "")

        if company:
            customer_name = company
        else:
            customer_name = f"{first_name} {last_name}".strip()

        if not customer_name:
            customer_name = email or f"Shopware Customer {self.customer_id[:8]}"

        customer_group = self.setting.customer_group

        # Name the Customer after Shopware's human-facing customer number
        # (e.g. "17722") instead of the raw internal UUID — same reasoning
        # as Sales Order using the Shopware order number.
        customer_number = customer.get("customerNumber")

        # Use base class to create customer
        super().sync_customer(customer_name, customer_group, set_name=customer_number)

        customer_doc = self.get_customer_doc()
        if email:
            customer_doc.email_id = email

        vat_id = None
        if customer.get("vatIds"):
            vat_ids = customer.get("vatIds", [])
            if vat_ids:
                vat_id = vat_ids[0]
                customer_doc.tax_id = vat_id

        # Shopware accountType: "business" or "private"
        account_type = customer.get("accountType", "")
        is_business = account_type == "business" or bool(company)
        customer_doc.customer_type = resolve_customer_type(is_business)

        # Multi-Storefront: Track source sales channel (where customer first registered)
        source_channel_id = sales_channel_id or customer.get("salesChannelId", "")
        if source_channel_id:
            customer_doc.shopware_source_sales_channel_id = source_channel_id
            for sc in (self.setting.sales_channels or []):
                if sc.sales_channel_id == source_channel_id:
                    customer_doc.shopware_source_sales_channel_name = sc.sales_channel_name
                    break

        customer_doc.save(ignore_permissions=True)

        if vat_id:
            trigger_vat_id_check(customer_doc.name, vat_id)

        # Create addresses
        billing_address = customer.get("defaultBillingAddress") or {}
        shipping_address = customer.get("defaultShippingAddress") or {}

        same_address = (
            not shipping_address
            or shipping_address.get("id") == billing_address.get("id")
        )

        if billing_address:
            self.create_customer_address(
                customer_name, billing_address, address_type="Billing", email=email,
                is_also_shipping=same_address,
            )
        if shipping_address and not same_address:
            self.create_customer_address(
                customer_name, shipping_address, address_type="Shipping", email=email,
            )

        self.create_customer_contact(customer)

    def create_customer_address(
        self,
        customer_name: str,
        shopware_address: dict[str, Any],
        address_type: str = "Billing",
        email: str | None = None,
        is_also_shipping: bool = False,
    ) -> None:
        """Create customer address using Shopware address data.

        Args:
            is_also_shipping: If True, set is_shipping_address=1 even for Billing type
                              (used when billing and shipping are the same address)
        """
        logger = get_logger("shopware_customer_address")
        shopware_address_id = (shopware_address or {}).get("id", "")
        logger.info(
            f"Address mapping start: customer_id={self.customer_id}, type={address_type}, "
            f"shopware_address_id={shopware_address_id}"
        )
        map_started_at = perf_counter()
        address_fields = _map_address_fields(
            shopware_address, customer_name, address_type, email, is_also_shipping
        )
        logger.info(
            f"Address mapping done in {perf_counter() - map_started_at:.2f}s: "
            f"customer_id={self.customer_id}, type={address_type}, shopware_address_id={shopware_address_id}"
        )

        insert_started_at = perf_counter()
        logger.info(
            f"Address insert start: customer_id={self.customer_id}, type={address_type}, "
            f"shopware_address_id={shopware_address_id}"
        )
        super().create_customer_address(address_fields)
        logger.info(
            f"Address insert done in {perf_counter() - insert_started_at:.2f}s: "
            f"customer_id={self.customer_id}, type={address_type}, shopware_address_id={shopware_address_id}"
        )

    def update_customer_data(self, customer: dict[str, Any]) -> None:
        """Update existing customer's core data from Shopware.

        Updates customer_type, tax_id, email, and triggers VAT ID check if a
        new VAT ID is set.
        """
        customer_doc = self.get_customer_doc()
        if not customer_doc:
            return

        update_dict: dict[str, Any] = {}
        updates_made: list[str] = []

        company = customer.get("company", "") or ""
        account_type = customer.get("accountType", "")
        is_business = account_type == "business" or bool(company)
        new_customer_type = resolve_customer_type(is_business)

        if customer_doc.customer_type != new_customer_type:
            update_dict["customer_type"] = new_customer_type
            updates_made.append(f"customer_type: {new_customer_type}")

        vat_id = None
        if customer.get("vatIds"):
            vat_ids = customer.get("vatIds", [])
            if vat_ids:
                vat_id = vat_ids[0]

        if vat_id and customer_doc.tax_id != vat_id:
            update_dict["tax_id"] = vat_id
            updates_made.append(f"tax_id: {vat_id}")

        email = customer.get("email", "")
        if email and customer_doc.email_id != email:
            update_dict["email_id"] = email
            updates_made.append(f"email_id: {email}")

        if update_dict:
            # Use set_value to avoid triggering doc_events (e.g. Chatwoot sync
            # on_update hook) which can hang on HTTP requests and block the
            # entire DB transaction, causing lock wait timeouts
            frappe.db.set_value("Customer", customer_doc.name, update_dict, update_modified=True)

            frappe.logger("shopware6").info(
                f"Updated customer {customer_doc.name}: {', '.join(updates_made)}"
            )

            if vat_id and f"tax_id: {vat_id}" in updates_made:
                trigger_vat_id_check(customer_doc.name, vat_id)

    def update_existing_addresses(self, customer: dict[str, Any]) -> None:
        """Update existing addresses with new data from Shopware."""
        billing_address = customer.get("defaultBillingAddress") or {}
        shipping_address = customer.get("defaultShippingAddress") or {}

        first_name = customer.get("firstName", "")
        last_name = customer.get("lastName", "")
        company = customer.get("company", "")
        customer_name = company or f"{first_name} {last_name}".strip()
        email = customer.get("email")

        same_address = (
            not shipping_address
            or shipping_address.get("id") == billing_address.get("id")
        )

        if billing_address:
            self._update_existing_address(
                customer_name, billing_address, "Billing", email, is_also_shipping=same_address
            )
        if shipping_address and not same_address:
            self._update_existing_address(customer_name, shipping_address, "Shipping", email)

    def _update_existing_address(
        self,
        customer_name: str,
        shopware_address: dict[str, Any],
        address_type: str = "Billing",
        email: str | None = None,
        is_also_shipping: bool = False,
    ) -> None:
        """Update existing address or create new one based on Shopware address ID.

        Only updates if the Shopware address ID matches. If different address ID,
        creates a new address to preserve address history (important for PayPal
        orders where address may change between orders).
        """
        if not shopware_address:
            return

        new_shopware_address_id = shopware_address.get("id", "")
        old_address = self.get_customer_address_doc(address_type)

        if not old_address:
            self.create_customer_address(
                customer_name, shopware_address, address_type, email, is_also_shipping
            )
            return

        old_shopware_address_id = getattr(old_address, ADDRESS_ID_FIELD, "") or ""

        if (
            old_shopware_address_id
            and new_shopware_address_id
            and old_shopware_address_id != new_shopware_address_id
        ):
            # Different Shopware address ID - this is a NEW address, not an update
            existing_with_id = frappe.db.sql(
                """
                SELECT a.name
                FROM `tabAddress` a
                INNER JOIN `tabDynamic Link` dl ON dl.parent = a.name AND dl.parenttype = 'Address'
                WHERE dl.link_doctype = 'Customer'
                AND dl.link_name = %s
                AND a.shopware_address_id = %s
                LIMIT 1
                """,
                (self.get_customer_doc().name, new_shopware_address_id),
                as_dict=True,
            )

            if not existing_with_id:
                self.create_customer_address(
                    customer_name, shopware_address, address_type, email, is_also_shipping
                )
                frappe.logger("shopware6").info(
                    f"Created new {address_type} address for customer {customer_name} "
                    f"(Shopware ID: {new_shopware_address_id}, old was {old_shopware_address_id})"
                )
        else:
            # Same Shopware address ID or no ID tracking - update existing
            exclude_in_update = ["address_title", "address_type"]
            new_values = _map_address_fields(
                shopware_address, customer_name, address_type, email, is_also_shipping
            )

            old_address.update({k: v for k, v in new_values.items() if k not in exclude_in_update})
            old_address.flags.ignore_mandatory = True
            old_address.save(ignore_permissions=True)

    def create_customer_contact(self, shopware_customer: dict[str, Any]) -> None:
        """Create contact from Shopware customer data."""
        first_name = shopware_customer.get("firstName", "")
        last_name = shopware_customer.get("lastName", "")
        email = shopware_customer.get("email", "")

        if not (first_name or email):
            return

        salutation, gender = map_shopware_salutation(shopware_customer.get("salutation", {}))

        contact_fields = {
            "status": "Passive",
            "first_name": first_name or "Customer",
            "last_name": last_name,
            "salutation": salutation,
            "gender": gender,
            "is_primary_contact": 1,
        }

        if email:
            contact_fields["email_ids"] = [{"email_id": email, "is_primary": True}]

        try:
            customer_doc = self.get_customer_doc()
            existing_contact = frappe.db.sql(
                """
                SELECT c.name FROM `tabContact` c
                INNER JOIN `tabDynamic Link` dl ON dl.parent = c.name AND dl.parenttype = 'Contact'
                WHERE dl.link_doctype = 'Customer' AND dl.link_name = %s
                LIMIT 1
                """,
                (customer_doc.name,),
                as_dict=True,
            )

            if existing_contact:
                contact = frappe.get_doc("Contact", existing_contact[0].name)
                updates: dict[str, Any] = {}
                # B2B fix: ERPNext's native create_primary_contact builds a
                # Company-type contact with only company_name — first/last stay
                # empty. Backfill the person's name from Shopware so the business
                # customer's contact isn't just the company name.
                if first_name and not contact.first_name:
                    updates["first_name"] = first_name
                if last_name and not contact.last_name:
                    updates["last_name"] = last_name
                if salutation and not contact.salutation:
                    updates["salutation"] = salutation
                if gender and not contact.gender:
                    updates["gender"] = gender
                if updates:
                    frappe.db.set_value("Contact", contact.name, updates)
                return
        except frappe.DoesNotExistError:
            pass

        logger = get_logger("shopware_customer_contact")
        contact_started_at = perf_counter()
        logger.info(
            f"Contact insert start: customer_id={self.customer_id}, email={email or 'n/a'}"
        )
        super().create_customer_contact(contact_fields)
        logger.info(
            f"Contact insert done in {perf_counter() - contact_started_at:.2f}s: "
            f"customer_id={self.customer_id}, email={email or 'n/a'}"
        )

    def create_or_update_billing_contact(
        self,
        billing_email: str,
        customer_data: dict[str, Any] | None = None,
        billing_name: str | None = None,
    ) -> str | None:
        """
        Create or update a Billing Contact for this customer.

        The billing contact has ``is_billing_contact=1`` and is used for
        invoice emails. ``billing_name`` overrides the contact's first name
        (used by callers that want a label like "Accounts Payable" instead of
        the customer's personal name).
        """
        if not billing_email:
            return None

        billing_email = billing_email.strip().lower()
        customer_doc = self.get_customer_doc()

        billing_contacts = frappe.db.sql(
            """
            SELECT c.name, c.email_id
            FROM `tabContact` c
            INNER JOIN `tabDynamic Link` dl ON dl.parent = c.name AND dl.parenttype = 'Contact'
            WHERE dl.link_doctype = 'Customer'
            AND dl.link_name = %s
            AND c.is_billing_contact = 1
            """,
            (customer_doc.name,),
            as_dict=True,
        )

        if billing_contacts:
            contact_name = billing_contacts[0].name
            contact = frappe.get_doc("Contact", contact_name)

            current_emails = [e.email_id.lower() for e in contact.email_ids if e.email_id]

            if billing_email not in current_emails:
                contact.email_ids = []
                contact.append("email_ids", {"email_id": billing_email, "is_primary": 1})
                contact.save(ignore_permissions=True)
                frappe.logger("shopware6").info(
                    f"Updated billing contact {contact_name} with new email {billing_email}"
                )

            return contact_name

        # Create new billing contact
        # TODO: read fallback from setting.fallback_billing_first_name once
        # Track E adds the field to Shopware Setting (default "Billing").
        fallback_first_name = getattr(self.setting, "fallback_billing_first_name", "Billing")
        first_name = fallback_first_name
        last_name = ""
        salutation = None
        gender = None

        if customer_data:
            first_name = customer_data.get("firstName") or fallback_first_name
            last_name = customer_data.get("lastName", "")
            salutation, gender = map_shopware_salutation(customer_data.get("salutation", {}))

        if billing_name:
            # Explicit override (e.g. "Accounts Payable") wins over names
            # derived from the customer payload.
            first_name = billing_name
            last_name = ""

        contact = frappe.get_doc({
            "doctype": "Contact",
            "first_name": first_name,
            "last_name": last_name,
            "salutation": salutation,
            "gender": gender,
            "is_primary_contact": 0,
            "is_billing_contact": 1,
            "email_ids": [{"email_id": billing_email, "is_primary": 1}],
            "links": [{"link_doctype": "Customer", "link_name": customer_doc.name}],
        })

        contact.insert(ignore_permissions=True)
        frappe.logger("shopware6").info(
            f"Created billing contact {contact.name} for {customer_doc.name} with email {billing_email}"
        )
        return contact.name
