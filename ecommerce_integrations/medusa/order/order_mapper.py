"""Map Medusa order JSON to ERPNext Sales Order dict."""
import frappe
from ecommerce_integrations.medusa.constants import CUSTOMER_ID_FIELD, ORDER_ID_FIELD, PRODUCT_ID_FIELD
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
    shipping_addr = order.get("shipping_address", {})
    if shipping_addr:
        so["shipping_address_name"] = _get_or_create_address(shipping_addr, customer_name)
    return so


def _resolve_customer(order: dict, setting) -> str:
    customer_id = order.get("customer_id")
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
        item_code = _resolve_item_code(product_id, variant.get("sku", ""), item)
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


def _resolve_item_code(product_id: str, sku: str, item: dict) -> str:
    if product_id:
        item_code = frappe.db.get_value("Item", {PRODUCT_ID_FIELD: product_id})
        if item_code:
            return item_code
    if sku:
        item_code = frappe.db.get_value("Item", {"item_code": sku})
        if item_code:
            return item_code
    title = item.get("title", "")
    if title:
        item_code = frappe.db.get_value("Item", {"item_name": title})
        if item_code:
            return item_code
    frappe.log_error(f"Could not resolve ERPNext item for Medusa product {product_id} / SKU {sku}", "Medusa Order Sync")
    return None


def _get_or_create_address(addr: dict, customer_name: str) -> str:
    from ecommerce_integrations.medusa.customer import _get_country_name
    existing = frappe.db.get_value("Address", {"address_line1": addr.get("address_1", ""), "city": addr.get("city", ""), "pincode": addr.get("postal_code", "")})
    if existing:
        return existing
    address = frappe.get_doc({
        "doctype": "Address",
        "address_title": f"{addr.get('first_name', '')} {addr.get('last_name', '')}".strip() or customer_name,
        "address_type": "Shipping",
        "address_line1": addr.get("address_1", ""),
        "address_line2": addr.get("address_2", ""),
        "city": addr.get("city", ""),
        "state": addr.get("province", ""),
        "pincode": addr.get("postal_code", ""),
        "country": _get_country_name(addr.get("country_code", "DE")),
        "phone": addr.get("phone", ""),
    })
    address.append("links", {"link_doctype": "Customer", "link_name": customer_name})
    address.flags.ignore_mandatory = True
    address.insert(ignore_permissions=True)
    return address.name
