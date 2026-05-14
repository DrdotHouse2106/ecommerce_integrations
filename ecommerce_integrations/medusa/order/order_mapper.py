"""Map Medusa order JSON to ERPNext Sales Order dict."""
import frappe
from ecommerce_integrations.ecommerce_integrations.checkout_utils import process_checkout_fields
from ecommerce_integrations.ecommerce_integrations.ecommerce_custom_fields import STOREFRONT_MEDUSA
from ecommerce_integrations.medusa.constants import CUSTOMER_ID_FIELD, MODULE_NAME as MEDUSA_MODULE, ORDER_ID_FIELD
from ecommerce_integrations.medusa.payment_method_mapping import (
    extract_provider_id,
    resolve_mode_of_payment,
)
from ecommerce_integrations.medusa.utils import medusa_price_to_erpnext


def map_medusa_order_to_so(order: dict, setting) -> dict:
    customer_name = _resolve_customer(order, setting)
    items = _map_line_items(order.get("items", []), setting)
    shipping_items = _map_shipping(order, setting)
    so = {
        "doctype": "Sales Order",
        "naming_series": setting.sales_order_series or "SO-.#####",
        ORDER_ID_FIELD: order.get("id"),
        "customer": customer_name,
        "company": setting.company,
        "transaction_date": frappe.utils.today(),
        "delivery_date": frappe.utils.add_days(frappe.utils.today(), 7),
        "items": items + shipping_items,
        "cost_center": setting.cost_center,
        "set_warehouse": setting.warehouse,
    }
    if setting.default_sales_tax_template:
        so["taxes_and_charges"] = setting.default_sales_tax_template

    billing_addr = order.get("billing_address") or {}
    shipping_addr = order.get("shipping_address") or {}
    customer_email = _resolve_email(order)

    if billing_addr:
        so["customer_address"] = _get_or_create_address(
            billing_addr, customer_name, address_type="Billing", email=customer_email,
        )
    if shipping_addr:
        so["shipping_address_name"] = _get_or_create_address(
            shipping_addr, customer_name, address_type="Shipping", email=customer_email,
        )

    contact_name = _get_or_create_contact(order, customer_name)
    if contact_name:
        so["contact_person"] = contact_name
    if customer_email:
        # Set explicitly as a fallback; ERPNext also fetches via contact_person.
        so["contact_email"] = customer_email

    # Unified ecommerce fields
    so["ecommerce_source"] = STOREFRONT_MEDUSA
    sales_channel = order.get("sales_channel") or {}
    sales_channel_id = sales_channel.get("id") or order.get("sales_channel_id", "")
    so["ecommerce_sales_channel_id"] = sales_channel_id
    channel_name = sales_channel.get("name") or _resolve_sales_channel_name(setting, sales_channel_id)
    so["ecommerce_sales_channel_name"] = channel_name

    # Pick up the channel's preferred language so emails + PDFs render in it.
    if channel_name:
        brand_language = frappe.db.get_value(
            "Ecommerce Channel Branding", channel_name, "language"
        )
        if brand_language:
            so["language"] = brand_language

    provider_id = extract_provider_id(order)
    if provider_id:
        so["medusa_payment_provider"] = provider_id
        mop = resolve_mode_of_payment(provider_id, setting)
        if mop:
            so["mode_of_payment"] = mop

    process_checkout_fields(so, setting, order, _extract_metadata_value, customer=customer_name)
    return so


def _resolve_email(order: dict) -> str:
    """Email used for the customer Contact and SO contact_email."""
    customer = order.get("customer") or {}
    return (customer.get("email") or order.get("email") or "").strip()


def _resolve_sales_channel_name(setting, sales_channel_id: str) -> str:
    if not sales_channel_id:
        return ""
    for sc in setting.sales_channels or []:
        if sc.sales_channel_id == sales_channel_id:
            return sc.sales_channel_name
    return ""


def _resolve_customer(order: dict, setting) -> str:
    customer = order.get("customer") or {}
    customer_id = customer.get("id") or order.get("customer_id")
    if customer_id:
        customer_name = frappe.db.get_value("Customer", {CUSTOMER_ID_FIELD: customer_id})
        if customer_name:
            return customer_name
    if setting.default_customer:
        return setting.default_customer
    frappe.throw(f"No customer found for Medusa order {order.get('id')}")


def _map_line_items(items: list, setting) -> list:
    so_items = []
    for item in items:
        variant = item.get("variant", {}) or {}
        product = variant.get("product", {}) or {}
        product_id = product.get("id") or variant.get("product_id", "")
        variant_id = variant.get("id") or item.get("variant_id", "")
        item_code = _resolve_item_code(product_id, variant.get("sku", "") or item.get("variant_sku", ""), item, variant_id)
        if not item_code:
            continue
        unit_price = medusa_price_to_erpnext(item.get("unit_price", 0))
        quantity = item.get("quantity", 1)
        so_item = {
            "item_code": item_code,
            "qty": quantity,
            "rate": unit_price,
            "warehouse": setting.warehouse,
        }
        discount = item.get("discount_total", 0)
        if discount:
            so_item["discount_amount"] = medusa_price_to_erpnext(discount)
        so_items.append(so_item)
    return so_items


def _map_shipping(order: dict, setting) -> list:
    if not setting.add_shipping_as_item or not setting.shipping_item:
        return []
    shipping_total = 0
    for method in order.get("shipping_methods", []):
        shipping_total += method.get("amount", 0)
    if not shipping_total:
        return []
    return [{"item_code": setting.shipping_item, "qty": 1, "rate": medusa_price_to_erpnext(shipping_total), "warehouse": setting.warehouse}]


def _resolve_item_code(product_id: str, sku: str, item: dict, variant_id: str = "") -> str:
    if variant_id:
        item_code = frappe.db.get_value(
            "Ecommerce Item",
            {"integration": MEDUSA_MODULE, "variant_id": variant_id, "has_variants": 0},
            "erpnext_item_code",
        )
        if item_code:
            return item_code
    if sku:
        row = frappe.db.get_value(
            "Item", {"item_code": sku}, ["name", "has_variants"], as_dict=True
        )
        if row and not row.has_variants:
            return row.name
    if product_id:
        rows = frappe.db.get_all(
            "Ecommerce Item",
            filters={
                "integration": MEDUSA_MODULE,
                "integration_item_code": product_id,
                "has_variants": 0,
            },
            fields=["erpnext_item_code"],
            limit=2,
        )
        if len(rows) == 1:
            return rows[0].erpnext_item_code
    title = item.get("title", "") or item.get("product_title", "")
    if title:
        row = frappe.db.get_value(
            "Item", {"item_name": title}, ["name", "has_variants"], as_dict=True
        )
        if row and not row.has_variants:
            return row.name
    frappe.log_error(
        f"Could not resolve ERPNext variant item for Medusa product {product_id} / variant {variant_id} / SKU {sku}",
        "Medusa Order Sync",
    )
    return None


def _get_or_create_address(addr: dict, customer_name: str, address_type: str = "Shipping", email: str = "") -> str:
    """Find an Address with the same line1+city+pincode or create a new one.

    The address_type passed in determines whether the new doc gets
    is_primary_address (Billing) or is_shipping_address (Shipping). Existing
    addresses are reused as-is — we only ensure they are linked to the customer.
    """
    from ecommerce_integrations.medusa.customer import _get_country_name
    existing = frappe.db.get_value(
        "Address",
        {
            "address_line1": addr.get("address_1", ""),
            "city": addr.get("city", ""),
            "pincode": addr.get("postal_code", ""),
        },
    )
    if existing:
        _ensure_address_link(existing, customer_name)
        return existing

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
        "email_id": email or "",
        "is_primary_address": 1 if address_type == "Billing" else 0,
        "is_shipping_address": 1 if address_type == "Shipping" else 0,
    })
    address.append("links", {"link_doctype": "Customer", "link_name": customer_name})
    address.flags.ignore_mandatory = True
    address.insert(ignore_permissions=True)
    return address.name


def _ensure_address_link(address_name: str, customer_name: str):
    """Ensure the address has a link to the given customer."""
    has_link = frappe.db.exists("Dynamic Link", {
        "parent": address_name,
        "parenttype": "Address",
        "link_doctype": "Customer",
        "link_name": customer_name,
    })
    if not has_link:
        address = frappe.get_doc("Address", address_name)
        address.append("links", {"link_doctype": "Customer", "link_name": customer_name})
        address.save(ignore_permissions=True)


def _get_or_create_contact(order: dict, customer_name: str) -> str | None:
    """Find or create a Contact for the order's customer email and link it.

    Returns the Contact name, suitable for ``Sales Order.contact_person`` —
    Frappe then auto-populates ``contact_email`` and ``contact_display`` on
    the SO when set_missing_values runs.
    """
    customer = order.get("customer") or {}
    email = (customer.get("email") or order.get("email") or "").strip()
    if not email:
        return None

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
        ORDER BY ce.is_primary DESC
        LIMIT 1
        """,
        (customer_name, email.lower()),
    )
    if existing:
        return existing[0][0]

    billing = order.get("billing_address") or {}
    first_name = (
        customer.get("first_name") or billing.get("first_name") or email.split("@")[0]
    )
    last_name = customer.get("last_name") or billing.get("last_name") or ""
    phone = customer.get("phone") or billing.get("phone") or ""

    contact = frappe.get_doc({
        "doctype": "Contact",
        "first_name": first_name,
        "last_name": last_name,
    })
    contact.append("email_ids", {"email_id": email, "is_primary": 1})
    if phone:
        contact.append("phone_nos", {"phone": phone, "is_primary_phone": 1, "is_primary_mobile_no": 1})
    contact.append("links", {"link_doctype": "Customer", "link_name": customer_name})
    contact.flags.ignore_mandatory = True
    contact.insert(ignore_permissions=True)
    return contact.name


def _extract_metadata_value(order: dict, field_names: list):
    """Extract a value from Medusa order metadata by searching multiple keys.

    Searches in: order.metadata, order.customer.metadata, then top-level order fields.
    """
    metadata = order.get("metadata", {}) or {}
    for name in field_names:
        if name in metadata:
            return metadata[name]

    customer_meta = (order.get("customer", {}) or {}).get("metadata", {}) or {}
    for name in field_names:
        if name in customer_meta:
            return customer_meta[name]

    for name in field_names:
        if name in order:
            return order[name]

    return None
