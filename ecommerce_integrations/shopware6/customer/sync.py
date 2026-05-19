"""
Shopware 6 Customer — sync entry points.

Webhook handlers, scheduled jobs, and the order-driven customer factory all
live here. They orchestrate the ``ShopwareCustomer`` class plus the lock /
VAT / mapping helpers in sibling modules.
"""

from typing import Any

import frappe
from frappe import _
from lib_shopware6_api_base import Criteria, Shopware6AdminAPIClientBase

from ecommerce_integrations.shopware6.connection import (
    get_shopware_client,
    temp_shopware_session,
)
from ecommerce_integrations.shopware6.constants import (
    CUSTOMER_ID_FIELD,
    SETTING_DOCTYPE,
)
from ecommerce_integrations.shopware6.utils import (
    get_logger,
    update_shopware_log,
)

from .core import (
    CustomerSyncInProgressError,
    ShopwareCustomer,
    _acquire_customer_sync_lock,
)
from .mapping import create_customer_contact
from .vat import _find_existing_customer_to_link


def _build_customer_criteria(*, limit: int | None = None, ids: list[str] | None = None) -> Criteria:
    """Build a Criteria that pulls customers with salutation + default addresses
    (incl. country + state) — shared by ``sync_customer_by_id``,
    ``sync_customers_from_shopware`` and ``sync_old_customers``.
    """
    if ids is not None:
        criteria = Criteria(ids=ids)
    elif limit is not None:
        criteria = Criteria(limit=int(limit))
    else:
        criteria = Criteria()

    criteria.associations["salutation"] = Criteria()
    for default in ("defaultBillingAddress", "defaultShippingAddress"):
        criteria.associations[default] = Criteria()
        criteria.associations[default].associations["country"] = Criteria()
        criteria.associations[default].associations["countryState"] = Criteria()
    return criteria


def get_customer_from_shopware_order(order: dict[str, Any]) -> str:
    """
    Get or create ERPNext Customer from Shopware order data.

    Uses the ShopwareCustomer class following the Shopify pattern:
    1. Check if customer is synced
    2. If not, sync the customer
    3. If yes, update addresses if needed
    """
    setting = frappe.get_doc(SETTING_DOCTYPE)

    order_customer = order.get("orderCustomer", {})
    if not order_customer:
        return setting.default_customer or create_guest_customer()

    customer_id = order_customer.get("customerId")
    if not customer_id:
        return setting.default_customer or create_guest_customer()

    nested_customer = order_customer.get("customer", {}) or {}

    customer_data = {
        "id": customer_id,
        "firstName": order_customer.get("firstName", ""),
        "lastName": order_customer.get("lastName", ""),
        "email": order_customer.get("email", ""),
        "company": order_customer.get("company", "") or nested_customer.get("company", ""),
        "salutation": order_customer.get("salutation", {}),
        "salutationId": order_customer.get("salutationId"),
        "accountType": nested_customer.get("accountType", ""),
        "guest": nested_customer.get("guest", False),
        "defaultBillingAddress": order.get("billingAddress"),
        "defaultShippingAddress": order.get("shippingAddress")
        or (
            order.get("deliveries", [{}])[0].get("shippingOrderAddress")
            if order.get("deliveries")
            else None
        ),
    }

    # VAT ID from billing address or nested customer
    billing_address = order.get("billingAddress", {}) or {}
    if billing_address.get("vatId"):
        customer_data["vatIds"] = [billing_address.get("vatId")]
    elif nested_customer.get("vatIds"):
        customer_data["vatIds"] = nested_customer.get("vatIds")

    logger = get_logger("get_customer_from_shopware_order")
    try:
        with _acquire_customer_sync_lock(customer_id):
            customer = ShopwareCustomer(customer_id=customer_id)

            sales_channel_id = order.get("salesChannelId", "")

            if not customer.is_synced():
                customer.sync_customer(customer_data, sales_channel_id=sales_channel_id)
            else:
                customer.update_customer_data(customer_data)
                customer.update_existing_addresses(customer_data)
                customer.create_customer_contact(customer_data)

            customer_name = customer.get_customer_doc().name
            logger.debug(
                f"Customer sync completed for customer_id={customer_id}, customer={customer_name}"
            )
            return customer_name
    except CustomerSyncInProgressError:
        existing_customer = frappe.db.get_value(
            "Customer", {CUSTOMER_ID_FIELD: customer_id}, "name"
        )
        if existing_customer:
            logger.info(
                f"Customer sync lock busy for customer_id={customer_id}; "
                f"reusing existing customer {existing_customer}"
            )
            return existing_customer
        raise


def ensure_customer_has_address(customer: str, order_data: dict[str, Any]) -> None:
    """
    Ensure customer has billing and shipping addresses from order data.

    This is especially important for PayPal Express orders where the address
    may not be available when the customer is initially created, but is
    included in the order data.
    """
    if not customer or not order_data:
        return

    # Importing here keeps `mapping` importable independent of the order modules
    from .mapping import _map_address_fields

    existing_addresses = frappe.db.sql(
        """
        SELECT a.name, a.address_type, a.is_primary_address, a.is_shipping_address
        FROM `tabAddress` a
        INNER JOIN `tabDynamic Link` dl ON dl.parent = a.name AND dl.parenttype = 'Address'
        WHERE dl.link_doctype = 'Customer' AND dl.link_name = %s
        """,
        (customer,),
        as_dict=True,
    )

    has_billing = any(
        addr.get("address_type") == "Billing" or addr.get("is_primary_address")
        for addr in existing_addresses
    )
    has_shipping = any(
        addr.get("address_type") == "Shipping" or addr.get("is_shipping_address")
        for addr in existing_addresses
    )

    billing_address = order_data.get("billingAddress") or {}
    shipping_address = order_data.get("shippingAddress") or (
        order_data.get("deliveries", [{}])[0].get("shippingOrderAddress")
        if order_data.get("deliveries")
        else None
    ) or {}

    order_customer = order_data.get("orderCustomer", {})
    first_name = order_customer.get("firstName", "")
    last_name = order_customer.get("lastName", "")
    company = order_customer.get("company", "")
    email = order_customer.get("email", "")
    customer_name = company or f"{first_name} {last_name}".strip() or customer

    same_address = (
        not shipping_address
        or shipping_address.get("id") == billing_address.get("id")
    )

    if not has_billing and billing_address and billing_address.get("street"):
        address_fields = _map_address_fields(
            billing_address, customer_name, "Billing", email, is_also_shipping=same_address
        )
        address_fields["links"] = [{"link_doctype": "Customer", "link_name": customer}]

        try:
            address = frappe.get_doc({"doctype": "Address", **address_fields})
            address.insert(ignore_permissions=True)
            frappe.logger("shopware6").info(
                f"Created billing address for {customer} from order data: {address.name}"
            )
            has_billing = True
            if same_address:
                has_shipping = True
        except Exception as e:
            frappe.logger("shopware6").warning(
                f"Failed to create billing address for {customer}: {e}"
            )

    if (
        not has_shipping
        and shipping_address
        and shipping_address.get("street")
        and not same_address
    ):
        address_fields = _map_address_fields(
            shipping_address, customer_name, "Shipping", email
        )
        address_fields["links"] = [{"link_doctype": "Customer", "link_name": customer}]

        try:
            address = frappe.get_doc({"doctype": "Address", **address_fields})
            address.insert(ignore_permissions=True)
            frappe.logger("shopware6").info(
                f"Created shipping address for {customer} from order data: {address.name}"
            )
        except Exception as e:
            frappe.logger("shopware6").warning(
                f"Failed to create shipping address for {customer}: {e}"
            )


def create_guest_customer() -> str:
    """Create or get the default guest customer."""
    guest_name = _("Shopware Guest Customer")

    if frappe.db.exists("Customer", guest_name):
        return guest_name

    setting = frappe.get_doc(SETTING_DOCTYPE)

    territory = frappe.db.get_single_value("Selling Settings", "territory")
    if not territory:
        territory = frappe.db.get_value("Territory", {"is_group": 0}, "name")
    if not territory:
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


def _ensure_customer_contact(customer: str, order_customer: dict[str, Any]) -> None:
    """Ensure a contact exists for the customer and update salutation/gender if needed."""
    from .mapping import map_shopware_salutation

    email = order_customer.get("email", "")
    salutation_data = order_customer.get("salutation", {})

    existing_contacts = frappe.db.sql(
        """
        SELECT DISTINCT c.name, c.salutation, c.gender, c.is_primary_contact
        FROM `tabContact` c
        INNER JOIN `tabDynamic Link` dl ON dl.parent = c.name AND dl.parenttype = 'Contact'
        WHERE dl.link_doctype = 'Customer'
        AND dl.link_name = %s
        ORDER BY c.is_primary_contact DESC, c.creation ASC
        LIMIT 1
        """,
        (customer,),
        as_dict=True,
    )

    if existing_contacts:
        contact = existing_contacts[0]
        salutation, gender = map_shopware_salutation(salutation_data)

        updates: dict[str, Any] = {}
        if salutation and contact.salutation != salutation:
            updates["salutation"] = salutation
        if gender and contact.gender != gender:
            updates["gender"] = gender
        if not contact.is_primary_contact:
            updates["is_primary_contact"] = 1

        if updates:
            frappe.db.set_value("Contact", contact.name, updates)
            frappe.logger("shopware6").info(
                f"Updated contact {contact.name} for {customer}: {updates}"
            )
    else:
        if email or salutation_data:
            create_customer_contact(customer, order_customer)


def sync_customer_from_webhook(payload: dict[str, Any], request_id: str | None = None):
    """Handle customer sync from Shopware webhook."""
    logger = get_logger("sync_customer_from_webhook")

    try:
        customer_id = payload.get("primaryKey") or payload.get("id")
        if not customer_id:
            entity = payload.get("payload", {}).get("entity", {})
            customer_id = entity.get("id")
        if not customer_id:
            # Custom webhook format from ERPNextWebhookSubscriber
            data = payload.get("data", {})
            customer_id = data.get("customerId")

            email = data.get("email")
            if customer_id and email is None:
                logger.info(
                    f"Skipping customer sync for {customer_id} - login event with null email "
                    "(likely guest or incomplete registration)"
                )
                if request_id:
                    update_shopware_log(
                        request_id,
                        status="Skipped",
                        message=f"Customer {customer_id} - login event with null email",
                    )
                return

        if customer_id:
            sync_customer_by_id(customer_id)

            if request_id:
                update_shopware_log(
                    request_id, status="Success", message=f"Synced customer {customer_id}"
                )
        else:
            if request_id:
                update_shopware_log(
                    request_id,
                    status="Error",
                    message="No customer ID in webhook payload",
                )

    except Exception as e:
        logger.error("Failed to sync customer from webhook", exception=e, persist=False)
        if request_id:
            update_shopware_log(request_id, status="Error", exception=str(e))
        raise


@temp_shopware_session
def sync_customer_by_id(
    client: Shopware6AdminAPIClientBase, customer_id: str
) -> str | None:
    """Sync a specific customer from Shopware by ID."""
    logger = get_logger("sync_customer_by_id")

    criteria = _build_customer_criteria(ids=[customer_id])
    response = client.request_post("search/customer", criteria)
    customers = response.get("data", [])

    if not customers:
        logger.info(f"Customer {customer_id} not found in Shopware (may have been deleted)")
        return None

    customer_data = customers[0]
    if not customer_data.get("email"):
        logger.warning(
            f"Customer {customer_id} has no email address - will use fallback naming"
        )

    return create_customer_from_shopware_data(customer_data)


def create_customer_from_shopware_data(customer_data: dict[str, Any]) -> str:
    """
    Create ERPNext Customer from Shopware customer data.

    Customer matching logic:
    1. First check by shopware_customer_id (exact match)
    2. For B2B (with company): Match by company name (not email - same email can have multiple companies)
    3. For B2C: Match by customer_name (firstName + lastName)
    4. Guest customers: Always create new customer (one-time orders)
    5. NEVER overwrite an existing shopware_customer_id (would break existing links)
    """
    customer_id = customer_data.get("id")

    if not customer_id:
        frappe.throw(_("Customer ID is required"))

    try:
        with _acquire_customer_sync_lock(customer_id):
            customer = ShopwareCustomer(customer_id=customer_id)

            sales_channel_id = customer_data.get("salesChannelId", "")

            if not customer.is_synced():
                email = customer_data.get("email", "")
                company = customer_data.get("company", "")
                first_name = customer_data.get("firstName", "")
                last_name = customer_data.get("lastName", "")
                is_guest = customer_data.get("guest", False)

                if company:
                    expected_customer_name = company
                else:
                    expected_customer_name = f"{first_name} {last_name}".strip()

                if is_guest:
                    frappe.logger("shopware6").info(
                        f"Creating new customer for guest order: {expected_customer_name} "
                        f"(Shopware ID: {customer_id})"
                    )
                    customer.sync_customer(customer_data, sales_channel_id=sales_channel_id)
                else:
                    existing_customer = _find_existing_customer_to_link(
                        email=email,
                        company=company,
                        customer_name=expected_customer_name,
                        customer_id=customer_id,
                    )

                    if existing_customer:
                        frappe.db.set_value(
                            "Customer", existing_customer, "shopware_customer_id", customer_id
                        )
                        if sales_channel_id:
                            frappe.db.set_value(
                                "Customer",
                                existing_customer,
                                "shopware_source_sales_channel_id",
                                sales_channel_id,
                            )
                            setting = frappe.get_doc(SETTING_DOCTYPE)
                            for sc in (setting.sales_channels or []):
                                if sc.sales_channel_id == sales_channel_id:
                                    frappe.db.set_value(
                                        "Customer",
                                        existing_customer,
                                        "shopware_source_sales_channel_name",
                                        sc.sales_channel_name,
                                    )
                                    break
                        frappe.logger("shopware6").info(
                            f"Linked existing customer '{existing_customer}' to Shopware ID {customer_id}"
                        )
                        return existing_customer
                    else:
                        customer.sync_customer(customer_data, sales_channel_id=sales_channel_id)
            else:
                customer.update_existing_addresses(customer_data)
                customer.create_customer_contact(customer_data)

            return customer.get_customer_doc().name
    except CustomerSyncInProgressError:
        existing_customer = frappe.db.get_value(
            "Customer", {CUSTOMER_ID_FIELD: customer_id}, "name"
        )
        if existing_customer:
            get_logger("create_customer_from_shopware_data").info(
                f"Customer sync lock busy for customer_id={customer_id}; "
                f"using existing customer {existing_customer}"
            )
            return existing_customer
        raise


@frappe.whitelist()
def sync_customers_from_shopware(limit: int = 100) -> dict[str, int]:
    """Manually sync customers from Shopware."""
    from ecommerce_integrations.shopware6.services.access import require_shopware_admin

    require_shopware_admin()
    client = get_shopware_client()

    criteria = _build_customer_criteria(limit=int(limit))
    response = client.request_post("search/customer", criteria)
    customers = response.get("data", [])

    synced = 0
    errors = 0

    for customer_data in customers:
        try:
            existing = frappe.db.get_value(
                "Customer", {"shopware_customer_id": customer_data.get("id")}, "name"
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
                persist=True,
            )

    return {"synced": synced, "errors": errors, "total": len(customers)}


@temp_shopware_session
def sync_old_customers(client: Shopware6AdminAPIClientBase):
    """Scheduled job: sync old customers from Shopware (hourly).

    Allows syncing customers that existed before the integration was set up.
    """
    from frappe.utils import cint

    setting = frappe.get_cached_doc(SETTING_DOCTYPE)

    if not cint(setting.sync_old_customers):
        return
    if not setting.enable_shopware:
        return

    try:
        limit = int(setting.old_customers_limit or 100)

        criteria = _build_customer_criteria(limit=limit)
        response = client.request_post("search/customer", criteria)
        customers = response.get("data", [])

        synced = 0
        skipped = 0
        errors = 0

        for customer_data in customers:
            try:
                existing = frappe.db.get_value(
                    "Customer",
                    {"shopware_customer_id": customer_data.get("id")},
                    "name",
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
                    persist=True,
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


# ---------------------------------------------------------------------- #
# B2G / XRechnung integration                                            #
# ---------------------------------------------------------------------- #


# EAS-code list entry that flags ``electronic_address`` as a German
# Leitweg-ID. See https://www.xrepository.de/details/urn:xoev-de:kosit:codeliste:eas
_LEITWEG_EAS_CODE = "0204"


def mirror_leitweg_into_electronic_address(doc, method=None):
    """Customer hook: copy ``leitweg_id`` into Alyf's ``electronic_address``.

    Operator surface keeps a friendly named ``Customer.leitweg_id`` field
    (declared in ``shopware6.custom_fields``). The eu_einvoice (Alyf)
    XRechnung renderer however reads from ``Customer.electronic_address``
    + ``electronic_address_scheme = '0204'``. Without this bridge the
    operator would have to type the Leitweg-ID twice.

    Rules:

    * Skip when ``leitweg_id`` is unset.
    * Skip when ``electronic_address`` already holds a *different* value
      so an operator override on Alyf's field isn't silently overwritten.
    * Only stamp ``electronic_address_scheme`` when it's currently empty;
      preserves manually-configured schemes like PEPPOL ``0192`` /
      GLN ``0088``.
    * No-op when Alyf's fields aren't installed on this site (the meta
      check keeps the hook portable across deployments).
    """
    leitweg = (getattr(doc, "leitweg_id", None) or "").strip()
    if not leitweg:
        return

    meta = doc.meta
    if not meta.has_field("electronic_address"):
        return  # eu_einvoice not installed

    current_address = (getattr(doc, "electronic_address", None) or "").strip()
    if not current_address:
        doc.electronic_address = leitweg
    elif current_address == leitweg:
        pass  # already in sync, nothing to do
    else:
        # Operator manually set a different electronic_address — don't
        # clobber. They may have a non-Leitweg use case here.
        return

    if meta.has_field("electronic_address_scheme"):
        current_scheme = (getattr(doc, "electronic_address_scheme", None) or "").strip()
        if not current_scheme:
            doc.electronic_address_scheme = _LEITWEG_EAS_CODE
