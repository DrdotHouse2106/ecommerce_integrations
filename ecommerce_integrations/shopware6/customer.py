"""
Shopware 6 Customer Sync Module

Handles synchronization of customers between Shopware 6 and ERPNext.
"""

from typing import Any, Dict, Optional

import frappe
from frappe import _
from frappe.utils import cstr

from lib_shopware6_api_base import (
    Shopware6AdminAPIClientBase,
    Criteria,
    EqualsFilter,
)

from ecommerce_integrations.controllers.customer import EcommerceCustomer
from ecommerce_integrations.shopware6.connection import temp_shopware_session, get_shopware_client
from ecommerce_integrations.shopware6.constants import (
    MODULE_NAME,
    SETTING_DOCTYPE,
    CUSTOMER_ID_FIELD,
    ADDRESS_ID_FIELD,
)
from ecommerce_integrations.shopware6.utils import (
    create_shopware_log,
    update_shopware_log,
    map_country_code,
    map_state_from_shopware,
    get_logger,
)


class ShopwareCustomer(EcommerceCustomer):
    """
    Shopware Customer handler following the same pattern as ShopifyCustomer.
    
    Uses the EcommerceCustomer base class for consistent customer handling
    across all integrations.
    """
    
    def __init__(self, customer_id: str):
        self.setting = frappe.get_doc(SETTING_DOCTYPE)
        super().__init__(customer_id, CUSTOMER_ID_FIELD, MODULE_NAME)
    
    def sync_customer(self, customer: Dict[str, Any], sales_channel_id: str = None) -> None:
        """Create Customer in ERPNext using Shopware's Customer dict."""

        # Determine customer name
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

        # Use base class to create customer
        super().sync_customer(customer_name, customer_group)

        # Update additional fields after creation
        customer_doc = self.get_customer_doc()
        if email:
            customer_doc.email_id = email

        vat_id = None
        if customer.get("vatIds"):
            vat_ids = customer.get("vatIds", [])
            if vat_ids:
                vat_id = vat_ids[0]
                customer_doc.tax_id = vat_id

        # Determine customer type based on Shopware accountType OR company field
        # Shopware accountType: "business" or "private"
        account_type = customer.get("accountType", "")
        is_business = account_type == "business" or bool(company)
        customer_doc.customer_type = "Company" if is_business else "Individual"

        # Multi-Storefront: Track source sales channel (where customer first registered)
        source_channel_id = sales_channel_id or customer.get("salesChannelId", "")
        if source_channel_id:
            customer_doc.shopware_source_sales_channel_id = source_channel_id
            # Try to get channel name from settings
            for sc in (self.setting.sales_channels or []):
                if sc.sales_channel_id == source_channel_id:
                    customer_doc.shopware_source_sales_channel_name = sc.sales_channel_name
                    break

        customer_doc.save(ignore_permissions=True)

        # Trigger VAT ID validation if VAT ID was provided
        if vat_id:
            trigger_vat_id_check(customer_doc.name, vat_id)
        
        # Create addresses
        billing_address = customer.get("defaultBillingAddress") or {}
        shipping_address = customer.get("defaultShippingAddress") or {}
        
        if billing_address:
            self.create_customer_address(
                customer_name, billing_address, address_type="Billing", email=email
            )
        if shipping_address and shipping_address.get("id") != billing_address.get("id"):
            self.create_customer_address(
                customer_name, shipping_address, address_type="Shipping", email=email
            )
        
        # Create contact
        self.create_customer_contact(customer)
    
    def create_customer_address(
        self,
        customer_name: str,
        shopware_address: Dict[str, Any],
        address_type: str = "Billing",
        email: str = None,
    ) -> None:
        """Create customer address using Shopware address data."""
        address_fields = _map_address_fields(shopware_address, customer_name, address_type, email)
        super().create_customer_address(address_fields)
    
    def update_customer_data(self, customer: Dict[str, Any]) -> None:
        """
        Update existing customer's core data from Shopware.

        Updates:
        - customer_type (Company/Individual) based on accountType
        - tax_id (VAT ID)
        - Triggers VAT ID check if new VAT ID is set

        Args:
            customer: Shopware customer data
        """
        customer_doc = self.get_customer_doc()
        if not customer_doc:
            return

        updates_made = []

        # Update accountType / customer_type
        company = customer.get("company", "") or ""
        account_type = customer.get("accountType", "")
        is_business = account_type == "business" or bool(company)
        new_customer_type = "Company" if is_business else "Individual"

        if customer_doc.customer_type != new_customer_type:
            customer_doc.customer_type = new_customer_type
            updates_made.append(f"customer_type: {new_customer_type}")

        # Update VAT ID
        vat_id = None
        if customer.get("vatIds"):
            vat_ids = customer.get("vatIds", [])
            if vat_ids:
                vat_id = vat_ids[0]

        if vat_id and customer_doc.tax_id != vat_id:
            customer_doc.tax_id = vat_id
            updates_made.append(f"tax_id: {vat_id}")

        # Update email if changed
        email = customer.get("email", "")
        if email and customer_doc.email_id != email:
            customer_doc.email_id = email
            updates_made.append(f"email_id: {email}")

        if updates_made:
            customer_doc.save(ignore_permissions=True)
            frappe.logger("shopware6").info(
                f"Updated customer {customer_doc.name}: {', '.join(updates_made)}"
            )

            # Trigger VAT ID check if VAT ID was added/changed
            if vat_id and f"tax_id: {vat_id}" in updates_made:
                trigger_vat_id_check(customer_doc.name, vat_id)

    def update_existing_addresses(self, customer: Dict[str, Any]) -> None:
        """Update existing addresses with new data from Shopware."""
        billing_address = customer.get("defaultBillingAddress") or {}
        shipping_address = customer.get("defaultShippingAddress") or {}

        first_name = customer.get("firstName", "")
        last_name = customer.get("lastName", "")
        company = customer.get("company", "")
        customer_name = company or f"{first_name} {last_name}".strip()
        email = customer.get("email")

        if billing_address:
            self._update_existing_address(customer_name, billing_address, "Billing", email)
        if shipping_address and shipping_address.get("id") != billing_address.get("id"):
            self._update_existing_address(customer_name, shipping_address, "Shipping", email)
    
    def _update_existing_address(
        self,
        customer_name: str,
        shopware_address: Dict[str, Any],
        address_type: str = "Billing",
        email: str = None,
    ) -> None:
        """Update a single existing address or create if not exists."""
        old_address = self.get_customer_address_doc(address_type)
        
        if not old_address:
            self.create_customer_address(customer_name, shopware_address, address_type, email)
        else:
            exclude_in_update = ["address_title", "address_type"]
            new_values = _map_address_fields(shopware_address, customer_name, address_type, email)
            
            old_address.update({k: v for k, v in new_values.items() if k not in exclude_in_update})
            old_address.flags.ignore_mandatory = True
            old_address.save(ignore_permissions=True)
    
    def create_customer_contact(self, shopware_customer: Dict[str, Any]) -> None:
        """Create contact from Shopware customer data."""
        first_name = shopware_customer.get("firstName", "")
        last_name = shopware_customer.get("lastName", "")
        email = shopware_customer.get("email", "")
        
        if not (first_name or email):
            return
        
        salutation, gender = map_shopware_salutation(shopware_customer.get("salutation", {}))
        
        contact_fields = {
            "status": "Passive",
            "first_name": first_name or "Kunde",
            "last_name": last_name,
            "salutation": salutation,
            "gender": gender,
            "is_primary_contact": 1,
        }
        
        if email:
            contact_fields["email_ids"] = [{"email_id": email, "is_primary": True}]
        
        # Check if contact already exists before creating
        try:
            customer_doc = self.get_customer_doc()
            existing_contact = frappe.db.sql("""
                SELECT c.name FROM `tabContact` c
                INNER JOIN `tabDynamic Link` dl ON dl.parent = c.name AND dl.parenttype = 'Contact'
                WHERE dl.link_doctype = 'Customer' AND dl.link_name = %s
                LIMIT 1
            """, (customer_doc.name,), as_dict=True)
            
            if existing_contact:
                # Update existing contact with salutation/gender if missing
                contact = frappe.get_doc("Contact", existing_contact[0].name)
                updates = {}
                if salutation and not contact.salutation:
                    updates["salutation"] = salutation
                if gender and not contact.gender:
                    updates["gender"] = gender
                if updates:
                    frappe.db.set_value("Contact", contact.name, updates)
                return
        except frappe.DoesNotExistError:
            pass
        
        super().create_customer_contact(contact_fields)
    
    def create_or_update_billing_contact(self, billing_email: str, customer_data: Dict[str, Any] = None) -> Optional[str]:
        """
        Create or update a Billing Contact for this customer.
        
        The billing contact has is_billing_contact=1 and is used for invoice emails.
        """
        if not billing_email:
            return None
        
        billing_email = billing_email.strip().lower()
        customer_doc = self.get_customer_doc()
        
        # Check if billing contact already exists
        billing_contacts = frappe.db.sql("""
            SELECT c.name, c.email_id
            FROM `tabContact` c
            INNER JOIN `tabDynamic Link` dl ON dl.parent = c.name AND dl.parenttype = 'Contact'
            WHERE dl.link_doctype = 'Customer' 
            AND dl.link_name = %s
            AND c.is_billing_contact = 1
        """, (customer_doc.name,), as_dict=True)
        
        if billing_contacts:
            # Update existing billing contact
            contact_name = billing_contacts[0].name
            contact = frappe.get_doc("Contact", contact_name)
            
            current_emails = [e.email_id.lower() for e in contact.email_ids if e.email_id]
            
            if billing_email not in current_emails:
                contact.email_ids = []
                contact.append("email_ids", {
                    "email_id": billing_email,
                    "is_primary": 1,
                })
                contact.save(ignore_permissions=True)
                frappe.logger("shopware6").info(
                    f"Updated billing contact {contact_name} with new email {billing_email}"
                )
            
            return contact_name
        
        # Create new billing contact
        first_name = "Buchhaltung"
        last_name = ""
        salutation = None
        gender = None
        
        if customer_data:
            first_name = customer_data.get("firstName") or "Buchhaltung"
            last_name = customer_data.get("lastName", "")
            salutation, gender = map_shopware_salutation(customer_data.get("salutation", {}))
        
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


def _map_address_fields(shopware_address: Dict[str, Any], customer_name: str, address_type: str, email: str = None) -> Dict[str, Any]:
    """Map Shopware address fields to ERPNext Address fields."""
    country_code = shopware_address.get("country", {}).get("iso", "") if isinstance(shopware_address.get("country"), dict) else ""
    country = map_country_code(country_code) or "Germany"

    state_data = shopware_address.get("countryState", {}) or {}
    state = map_state_from_shopware(state_data, country)

    # Avoid duplicate address type suffixes (e.g., "X GmbH-Shipping-Shipping")
    if customer_name.endswith(f"-{address_type}"):
        address_title = customer_name
    else:
        address_title = f"{customer_name}-{address_type}"

    address_fields = {
        "address_title": address_title,
        "address_type": address_type,
        ADDRESS_ID_FIELD: shopware_address.get("id"),
        "address_line1": shopware_address.get("street") or "Straße",
        "address_line2": shopware_address.get("additionalAddressLine1", ""),
        "city": shopware_address.get("city") or "Stadt",
        "state": state,
        "pincode": shopware_address.get("zipcode"),
        "country": country,
        "email_id": email,
    }
    
    phone = shopware_address.get("phoneNumber")
    if phone:
        address_fields["phone"] = phone
    
    return address_fields


def get_customer_from_shopware_order(order: Dict[str, Any]) -> str:
    """
    Get or create ERPNext Customer from Shopware order data.
    
    Uses the ShopwareCustomer class following the Shopify pattern:
    1. Check if customer is synced
    2. If not, sync the customer
    3. If yes, update addresses if needed

    Args:
        order: Shopware order data

    Returns:
        ERPNext Customer name
    """
    setting = frappe.get_doc(SETTING_DOCTYPE)

    order_customer = order.get("orderCustomer", {})
    if not order_customer:
        # Use default customer for guest orders
        return setting.default_customer or create_guest_customer()

    customer_id = order_customer.get("customerId")
    if not customer_id:
        return setting.default_customer or create_guest_customer()

    # Build customer data from order (similar to what Shopify does)
    # Get nested customer object if available (contains accountType)
    nested_customer = order_customer.get("customer", {}) or {}

    customer_data = {
        "id": customer_id,
        "firstName": order_customer.get("firstName", ""),
        "lastName": order_customer.get("lastName", ""),
        "email": order_customer.get("email", ""),
        "company": order_customer.get("company", "") or nested_customer.get("company", ""),
        "salutation": order_customer.get("salutation", {}),
        "salutationId": order_customer.get("salutationId"),
        "accountType": nested_customer.get("accountType", ""),  # "business" or "private"
        "defaultBillingAddress": order.get("billingAddress"),
        "defaultShippingAddress": order.get("shippingAddress") or (
            order.get("deliveries", [{}])[0].get("shippingOrderAddress")
            if order.get("deliveries") else None
        ),
    }

    # VAT ID from billing address or nested customer
    billing_address = order.get("billingAddress", {}) or {}
    if billing_address.get("vatId"):
        customer_data["vatIds"] = [billing_address.get("vatId")]
    elif nested_customer.get("vatIds"):
        customer_data["vatIds"] = nested_customer.get("vatIds")

    # Use ShopwareCustomer class (following Shopify pattern)
    customer = ShopwareCustomer(customer_id=customer_id)

    # Multi-Storefront: Get sales channel from order
    sales_channel_id = order.get("salesChannelId", "")

    if not customer.is_synced():
        # Customer not synced - create it with source sales channel
        customer.sync_customer(customer_data, sales_channel_id=sales_channel_id)
    else:
        # Customer exists - update core data, addresses and contact
        customer.update_customer_data(customer_data)
        customer.update_existing_addresses(customer_data)
        # Also ensure contact has salutation/gender
        customer.create_customer_contact(customer_data)

    return customer.get_customer_doc().name



def create_guest_customer() -> str:
    """Create or get the default guest customer."""
    guest_name = _("Shopware Guest Customer")

    if frappe.db.exists("Customer", guest_name):
        return guest_name

    setting = frappe.get_doc(SETTING_DOCTYPE)

    # Get default territory - try Selling Settings first, then fallback to any territory
    territory = frappe.db.get_single_value("Selling Settings", "territory")
    if not territory:
        # Fallback: get first available territory
        territory = frappe.db.get_value("Territory", {"is_group": 0}, "name")
    if not territory:
        # Last resort: get any territory
        territory = frappe.db.get_value("Territory", filters={}, fieldname="name")

    customer = frappe.get_doc({
        "doctype": "Customer",
        "customer_name": guest_name,
        "customer_type": "Individual",
        "customer_group": setting.customer_group,
        "territory": territory,
    })
    customer.insert(ignore_permissions=True)
    return customer.name


def _ensure_customer_contact(customer: str, order_customer: Dict[str, Any]) -> None:
    """
    Ensure a contact exists for the customer and update salutation/gender if needed.
    
    Args:
        customer: ERPNext Customer name
        order_customer: Shopware orderCustomer data with salutation
    """
    email = order_customer.get("email", "")
    salutation_data = order_customer.get("salutation", {})
    
    # Check if ANY contact already exists for this customer (not just primary)
    existing_contacts = frappe.db.sql("""
        SELECT DISTINCT c.name, c.salutation, c.gender, c.is_primary_contact
        FROM `tabContact` c
        INNER JOIN `tabDynamic Link` dl ON dl.parent = c.name AND dl.parenttype = 'Contact'
        WHERE dl.link_doctype = 'Customer' 
        AND dl.link_name = %s
        ORDER BY c.is_primary_contact DESC, c.creation ASC
        LIMIT 1
    """, (customer,), as_dict=True)
    
    if existing_contacts:
        # Contact exists - update salutation/gender if needed
        contact = existing_contacts[0]
        salutation, gender = map_shopware_salutation(salutation_data)
        
        updates = {}
        if salutation and contact.salutation != salutation:
            updates["salutation"] = salutation
        if gender and contact.gender != gender:
            updates["gender"] = gender
        # Ensure it's marked as primary
        if not contact.is_primary_contact:
            updates["is_primary_contact"] = 1
        
        if updates:
            frappe.db.set_value("Contact", contact.name, updates)
            frappe.logger("shopware6").info(
                f"Updated contact {contact.name} for {customer}: {updates}"
            )
    else:
        # No contact exists - create one
        if email or salutation_data:
            create_customer_contact(customer, order_customer)


def map_shopware_salutation(salutation_data: Dict[str, Any]) -> tuple:
    """
    Map Shopware salutation to ERPNext salutation and gender.

    Shopware salutationKey can be:
    - English: mr, mrs, ms, not_specified (Shopware default)
    - German: herr, frau, nicht_angegeben (custom German shops)

    ERPNext Gender: Male, Female, Other
    ERPNext Salutation: Herr, Frau, etc.

    Returns:
        tuple: (salutation, gender)
    """
    if not salutation_data:
        return None, None

    salutation_key = salutation_data.get("salutationKey", "") if isinstance(salutation_data, dict) else ""
    display_name = salutation_data.get("displayName", "") if isinstance(salutation_data, dict) else str(salutation_data)

    # Map salutation key to gender (supports both English and German keys)
    gender_map = {
        # English keys (Shopware default)
        "mr": "Male",
        "mrs": "Female",
        "ms": "Female",
        "not_specified": "Other",
        # German keys (custom shops)
        "herr": "Male",
        "frau": "Female",
        "nicht_angegeben": "Other",
        "divers": "Other",
    }

    # Map to ERPNext salutation (supports both English and German keys)
    salutation_map = {
        # English keys (Shopware default)
        "mr": "Herr",
        "mrs": "Frau",
        "ms": "Frau",
        "not_specified": None,
        # German keys (custom shops)
        "herr": "Herr",
        "frau": "Frau",
        "nicht_angegeben": None,
        "divers": None,
    }

    gender = gender_map.get(salutation_key.lower() if salutation_key else "", None)
    salutation = salutation_map.get(salutation_key.lower() if salutation_key else "", display_name)

    return salutation, gender


def create_customer_contact(customer: str, customer_data: Dict[str, Any], is_primary: bool = True) -> Optional[str]:
    """
    Create a Contact linked to the Customer.

    Args:
        customer: ERPNext Customer name
        customer_data: Shopware customer data
        is_primary: Whether this is the primary contact

    Returns:
        Contact name if created
    """
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
        "links": [{
            "link_doctype": "Customer",
            "link_name": customer,
        }],
    })

    if email:
        contact.append("email_ids", {
            "email_id": email,
            "is_primary": 1,
        })
    
    if phone:
        contact.append("phone_nos", {
            "phone": phone,
            "is_primary_phone": 1,
        })

    try:
        contact.insert(ignore_permissions=True)
        return contact.name
    except Exception as e:
        logger = get_logger("create_contact")
        logger.error(f"Failed to create contact for {customer}", exception=e, persist=False)
        return None


def create_or_update_billing_contact(
    customer: str, 
    billing_email: str, 
    billing_name: str = None,
    customer_data: Dict[str, Any] = None
) -> Optional[str]:
    """
    Create or update a Billing Contact for the Customer.
    
    This contact will have is_billing_contact=1 and will be used for invoice emails.
    If the billing email changes on future orders, the existing billing contact is updated.
    
    Args:
        customer: ERPNext Customer name
        billing_email: The billing email address
        billing_name: Optional name for the billing contact (defaults to "Buchhaltung")
        customer_data: Optional Shopware customer data for name/salutation
    
    Returns:
        Contact name if created/updated
    """
    if not billing_email:
        return None
    
    billing_email = billing_email.strip().lower()
    
    # Simpler approach: Find billing contact by querying Dynamic Link
    billing_contacts = frappe.db.sql("""
        SELECT DISTINCT c.name, c.email_id
        FROM `tabContact` c
        INNER JOIN `tabDynamic Link` dl ON dl.parent = c.name AND dl.parenttype = 'Contact'
        WHERE dl.link_doctype = 'Customer' 
        AND dl.link_name = %s
        AND c.is_billing_contact = 1
    """, (customer,), as_dict=True)
    
    if billing_contacts:
        # Update existing billing contact
        contact_name = billing_contacts[0].name
        contact = frappe.get_doc("Contact", contact_name)
        
        # Check if email needs to be updated
        current_emails = [e.email_id.lower() for e in contact.email_ids if e.email_id]
        
        if billing_email not in current_emails:
            # Clear old emails and set new billing email
            contact.email_ids = []
            contact.append("email_ids", {
                "email_id": billing_email,
                "is_primary": 1,
            })
            
            try:
                contact.save(ignore_permissions=True)
                logger = get_logger("create_or_update_billing_contact")
                logger.info(
                    f"Updated billing contact {contact_name} for {customer} with new email {billing_email}"
                )
            except Exception as e:
                logger = get_logger("create_or_update_billing_contact")
                logger.error(f"Failed to update billing contact for {customer}", exception=e, persist=False)
        
        return contact_name
    
    # Create new billing contact
    # Use customer data for name/salutation if available, otherwise use generic name
    first_name = "Buchhaltung"
    last_name = ""
    salutation = None
    gender = None
    
    if customer_data:
        # Use customer's name but mark as billing
        first_name = customer_data.get("firstName", "") or "Buchhaltung"
        last_name = customer_data.get("lastName", "")
        salutation_data = customer_data.get("salutation", {})
        salutation, gender = map_shopware_salutation(salutation_data)
    
    if billing_name:
        # If explicit billing name provided
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
        "links": [{
            "link_doctype": "Customer",
            "link_name": customer,
        }],
    })
    
    contact.append("email_ids", {
        "email_id": billing_email,
        "is_primary": 1,
    })
    
    try:
        contact.insert(ignore_permissions=True)
        frappe.logger("shopware6").info(
            f"Created billing contact {contact.name} for {customer} with email {billing_email}"
        )
        return contact.name
    except Exception as e:
        logger = get_logger("create_or_update_billing_contact")
        logger.error(f"Failed to create billing contact for {customer}", exception=e, persist=False)
        return None


def create_customer_address(
    customer: str,
    address_data: Dict[str, Any],
    address_type: str = "Billing"
) -> Optional[str]:
    """
    Create an Address linked to the Customer.

    Args:
        customer: ERPNext Customer name
        address_data: Shopware address data
        address_type: "Billing" or "Shipping"

    Returns:
        Address name if created
    """
    if not address_data:
        return None

    # Extract address fields
    street = address_data.get("street", "")
    additional = address_data.get("additionalAddressLine1", "")
    city = address_data.get("city", "")
    zipcode = address_data.get("zipcode", "")
    country_id = address_data.get("countryId", "")
    
    # Get phone number if available
    phone = address_data.get("phoneNumber", "")

    # Get country from Shopware country ID or ISO code
    country = address_data.get("country", {})
    country_iso = country.get("iso", "") if isinstance(country, dict) else ""
    country_name = map_country_code(country_iso) or _("Germany")
    
    # Get state/province (Bundesland)
    country_state = address_data.get("countryState", {})
    state_name = map_state_from_shopware(country_state, country_name)
    
    # Get email from address if available
    email = address_data.get("email", "")

    address_title = f"{customer}-{address_type}"

    # Check if address already exists
    if frappe.db.exists("Address", {"address_title": address_title}):
        return address_title

    address_doc = {
        "doctype": "Address",
        "address_title": address_title,
        "address_type": address_type,
        "address_line1": street,
        "address_line2": additional,
        "city": city,
        "pincode": zipcode,
        "country": country_name,
        "links": [{
            "link_doctype": "Customer",
            "link_name": customer,
        }],
    }
    
    # Add state if found
    if state_name:
        address_doc["state"] = state_name
    
    # Add phone if available
    if phone:
        address_doc["phone"] = phone
    
    # Add email if available (useful for billing address)
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


def sync_customer_from_webhook(payload: Dict[str, Any], request_id: str = None):
    """
    Handle customer sync from Shopware webhook.

    Args:
        payload: Webhook payload from Shopware
        request_id: Log entry ID for tracking
    """
    logger = get_logger("sync_customer_from_webhook")
    
    try:
        # Support multiple payload formats
        customer_id = payload.get("primaryKey") or payload.get("id")
        if not customer_id:
            entity = payload.get("payload", {}).get("entity", {})
            customer_id = entity.get("id")
        if not customer_id:
            # Custom webhook format from ERPNextWebhookSubscriber
            data = payload.get("data", {})
            customer_id = data.get("customerId")
            
            # Check if this is a login event with null email - these can be skipped
            email = data.get("email")
            if customer_id and email is None:
                logger.info(f"Skipping customer sync for {customer_id} - login event with null email (likely guest or incomplete registration)")
                if request_id:
                    update_shopware_log(request_id, status="Skipped", message=f"Customer {customer_id} - login event with null email")
                return

        if customer_id:
            sync_customer_by_id(customer_id)

            if request_id:
                update_shopware_log(request_id, status="Success", message=f"Synced customer {customer_id}")
        else:
            if request_id:
                update_shopware_log(request_id, status="Error", message="No customer ID in webhook payload")

    except Exception as e:
        logger.error(f"Failed to sync customer from webhook", exception=e, persist=False)
        if request_id:
            update_shopware_log(request_id, status="Error", exception=str(e))
        raise


@temp_shopware_session
def sync_customer_by_id(client: Shopware6AdminAPIClientBase, customer_id: str) -> Optional[str]:
    """
    Sync a specific customer from Shopware by ID.

    Args:
        client: Shopware API client
        customer_id: Shopware customer ID

    Returns:
        ERPNext Customer name if synced
    """
    logger = get_logger("sync_customer_by_id")
    
    criteria = Criteria(ids=[customer_id])
    criteria.associations["salutation"] = Criteria()
    criteria.associations["defaultBillingAddress"] = Criteria()
    criteria.associations["defaultBillingAddress"].associations["country"] = Criteria()
    criteria.associations["defaultBillingAddress"].associations["countryState"] = Criteria()
    criteria.associations["defaultShippingAddress"] = Criteria()
    criteria.associations["defaultShippingAddress"].associations["country"] = Criteria()
    criteria.associations["defaultShippingAddress"].associations["countryState"] = Criteria()

    response = client.request_post("search/customer", criteria)
    customers = response.get("data", [])

    if not customers:
        logger.info(f"Customer {customer_id} not found in Shopware (may have been deleted)")
        return None

    customer_data = customers[0]
    
    # Log if email is missing for debugging
    if not customer_data.get("email"):
        logger.warning(f"Customer {customer_id} has no email address - will use fallback naming")
    
    return create_customer_from_shopware_data(customer_data)


def create_customer_from_shopware_data(customer_data: Dict[str, Any]) -> str:
    """
    Create ERPNext Customer from Shopware customer data.
    
    Uses the ShopwareCustomer class following the Shopify pattern.

    Args:
        customer_data: Full Shopware customer object

    Returns:
        ERPNext Customer name
    """
    customer_id = customer_data.get("id")
    
    if not customer_id:
        frappe.throw(_("Customer ID is required"))
    
    # Use ShopwareCustomer class (following Shopify pattern)
    customer = ShopwareCustomer(customer_id=customer_id)

    # Multi-Storefront: Get sales channel ID from customer data
    sales_channel_id = customer_data.get("salesChannelId", "")

    if not customer.is_synced():
        # Check if customer exists by email first
        email = customer_data.get("email", "")
        if email:
            existing_by_email = frappe.db.get_value("Customer", {"email_id": email}, "name")
            if existing_by_email:
                # Link existing customer to Shopware ID
                frappe.db.set_value("Customer", existing_by_email, "shopware_customer_id", customer_id)
                # Also set source sales channel if available
                if sales_channel_id:
                    frappe.db.set_value("Customer", existing_by_email, "shopware_source_sales_channel_id", sales_channel_id)
                    setting = frappe.get_doc(SETTING_DOCTYPE)
                    for sc in (setting.sales_channels or []):
                        if sc.sales_channel_id == sales_channel_id:
                            frappe.db.set_value("Customer", existing_by_email, "shopware_source_sales_channel_name", sc.sales_channel_name)
                            break
                frappe.logger("shopware6").info(f"Linked existing customer {existing_by_email} (by email) to Shopware ID {customer_id}")
                return existing_by_email

        # Create new customer with source sales channel
        customer.sync_customer(customer_data, sales_channel_id=sales_channel_id)
    else:
        # Customer exists - update addresses if needed
        customer.update_existing_addresses(customer_data)
        # Also update contact salutation/gender
        customer.create_customer_contact(customer_data)
    
    return customer.get_customer_doc().name



@frappe.whitelist()
def sync_customers_from_shopware(limit: int = 100) -> Dict[str, int]:
    """
    Manually sync customers from Shopware.

    Args:
        limit: Maximum number of customers to sync

    Returns:
        dict: Sync results
    """
    client = get_shopware_client()

    criteria = Criteria(limit=int(limit))
    criteria.associations["salutation"] = Criteria()
    criteria.associations["defaultBillingAddress"] = Criteria()
    criteria.associations["defaultBillingAddress"].associations["country"] = Criteria()
    criteria.associations["defaultBillingAddress"].associations["countryState"] = Criteria()
    criteria.associations["defaultShippingAddress"] = Criteria()
    criteria.associations["defaultShippingAddress"].associations["country"] = Criteria()
    criteria.associations["defaultShippingAddress"].associations["countryState"] = Criteria()

    response = client.request_post("search/customer", criteria)
    customers = response.get("data", [])

    synced = 0
    errors = 0

    for customer_data in customers:
        try:
            existing = frappe.db.get_value(
                "Customer",
                {"shopware_customer_id": customer_data.get("id")},
                "name"
            )
            if not existing:
                create_customer_from_shopware_data(customer_data)
                synced += 1
        except Exception as e:
            errors += 1
            logger = get_logger("sync_customers_from_shopware")
            logger.error(
                f"Failed to sync customer {customer_data.get('id')}",
                exception=e,
                persist=True
            )

    return {
        "synced": synced,
        "errors": errors,
        "total": len(customers),
    }


@temp_shopware_session
def sync_old_customers(client: Shopware6AdminAPIClientBase):
    """
    Scheduled job to sync old customers from Shopware.
    Similar to the old orders sync functionality.
    This allows syncing customers that existed before the integration was set up.
    Called hourly by scheduler.
    """
    from frappe.utils import cint

    setting = frappe.get_cached_doc(SETTING_DOCTYPE)

    if not cint(setting.sync_old_customers):
        return

    if not setting.enable_shopware:
        return

    try:
        limit = int(setting.old_customers_limit or 100)

        criteria = Criteria(limit=limit)
        criteria.associations["salutation"] = Criteria()
        criteria.associations["defaultBillingAddress"] = Criteria()
        criteria.associations["defaultBillingAddress"].associations["country"] = Criteria()
        criteria.associations["defaultBillingAddress"].associations["countryState"] = Criteria()
        criteria.associations["defaultShippingAddress"] = Criteria()
        criteria.associations["defaultShippingAddress"].associations["country"] = Criteria()
        criteria.associations["defaultShippingAddress"].associations["countryState"] = Criteria()

        response = client.request_post("search/customer", criteria)
        customers = response.get("data", [])

        synced = 0
        skipped = 0
        errors = 0

        for customer_data in customers:
            try:
                existing = frappe.db.get_value(
                    "Customer", {"shopware_customer_id": customer_data.get("id")}, "name"
                )
                if existing:
                    skipped += 1
                    continue

                create_customer_from_shopware_data(customer_data)
                synced += 1

            except Exception as e:
                errors += 1
                logger = get_logger("scheduled_old_customer_sync")
                logger.error(
                    f"Failed to sync old customer {customer_data.get('id')}",
                    exception=e,
                    persist=True
                )

        # Disable the flag after successful sync (consistent with Shopify implementation)
        if errors == 0:
            setting = frappe.get_doc(SETTING_DOCTYPE)
            setting.sync_old_customers = 0
            setting.save(ignore_permissions=True)

        if synced > 0 or errors > 0:
            frappe.logger("shopware6").info(
                f"Old customer sync completed: {synced} synced, {skipped} skipped, {errors} errors"
            )

    except Exception as e:
        logger = get_logger("scheduled_old_customer_sync")
        logger.error("Scheduled old customer sync failed", exception=e, persist=True)


def trigger_vat_id_check(customer_name: str, vat_id: str) -> Optional[str]:
    """
    Trigger automatic VAT ID validation using ERPNext Germany's VAT ID Check.

    Creates a VAT ID Check document which will validate the VAT ID against
    the EU VIES database.

    Args:
        customer_name: ERPNext Customer name
        vat_id: VAT ID to validate (e.g., DE123456789)

    Returns:
        VAT ID Check document name if created, None on error
    """
    if not vat_id:
        return None

    # Clean VAT ID (remove spaces, uppercase)
    vat_id = vat_id.strip().upper().replace(" ", "")

    # Check if VAT ID Check doctype exists (Germany plugin installed)
    if not frappe.db.exists("DocType", "VAT ID Check"):
        frappe.logger("shopware6").info(
            f"VAT ID Check doctype not found - Germany plugin may not be installed. "
            f"Skipping VAT validation for {customer_name}"
        )
        return None

    # Check if a VAT ID Check already exists for this customer
    existing_check = frappe.db.get_value(
        "VAT ID Check",
        {
            "party_type": "Customer",
            "party": customer_name,
            "party_vat_id": vat_id
        },
        "name"
    )

    if existing_check:
        frappe.logger("shopware6").info(
            f"VAT ID Check already exists for {customer_name}: {existing_check}"
        )
        return existing_check

    try:
        # Get customer's billing address for validation
        billing_address = frappe.db.get_value(
            "Address",
            {
                "link_doctype": "Customer",
                "link_name": customer_name,
                "address_type": "Billing"
            },
            "name"
        )

        # If no billing address, try to get any address
        if not billing_address:
            billing_address = frappe.db.sql("""
                SELECT a.name
                FROM `tabAddress` a
                INNER JOIN `tabDynamic Link` dl ON dl.parent = a.name AND dl.parenttype = 'Address'
                WHERE dl.link_doctype = 'Customer' AND dl.link_name = %s
                LIMIT 1
            """, (customer_name,), as_dict=True)
            if billing_address:
                billing_address = billing_address[0].name

        # Create VAT ID Check document
        vat_check = frappe.get_doc({
            "doctype": "VAT ID Check",
            "party_type": "Customer",
            "party": customer_name,
            "party_vat_id": vat_id,
            "party_address": billing_address,
        })

        vat_check.insert(ignore_permissions=True)

        frappe.logger("shopware6").info(
            f"Created VAT ID Check {vat_check.name} for customer {customer_name} with VAT ID {vat_id}"
        )

        # Try to run the validation (if the method exists)
        try:
            if hasattr(vat_check, 'validate_vat_id'):
                vat_check.validate_vat_id()
                vat_check.save(ignore_permissions=True)
            elif hasattr(vat_check, 'check_vat_id'):
                vat_check.check_vat_id()
                vat_check.save(ignore_permissions=True)
        except Exception as validation_error:
            frappe.logger("shopware6").warning(
                f"VAT ID validation failed for {vat_id}: {validation_error}. "
                f"Check can be run manually: {vat_check.name}"
            )

        return vat_check.name

    except Exception as e:
        logger = get_logger("trigger_vat_id_check")
        logger.error(
            f"Failed to create VAT ID Check for {customer_name} with VAT ID {vat_id}",
            exception=e,
            persist=True
        )
        return None
