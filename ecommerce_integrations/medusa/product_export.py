"""Export ERPNext Items to Medusa v2 as Products."""
import frappe
from ecommerce_integrations.medusa.connection import medusa_request, temp_medusa_session
from ecommerce_integrations.medusa.constants import API_PRODUCTS, API_PRODUCT_VARIANTS, PRODUCT_ID_FIELD, SETTING_DOCTYPE, VARIANT_ID_FIELD
from ecommerce_integrations.medusa.utils import create_medusa_log, erpnext_price_to_medusa, is_medusa_enabled, update_medusa_log


class MedusaProductExporter:
    def __init__(self, item_code: str):
        self.item_code = item_code
        self.item = frappe.get_doc("Item", item_code)
        self.setting = frappe.get_cached_doc(SETTING_DOCTYPE)

    def get_medusa_product_id(self):
        return self.item.get(PRODUCT_ID_FIELD)

    def is_synced(self) -> bool:
        return bool(self.get_medusa_product_id())

    def export(self):
        if self.is_synced():
            self._update_product()
        else:
            self._create_product()

    @temp_medusa_session
    def _create_product(self, session, base_url):
        payload = self._build_product_payload()
        result = medusa_request(session, base_url, "POST", API_PRODUCTS, json=payload)
        product = result.get("product", {})
        medusa_id = product.get("id")
        if medusa_id:
            frappe.db.set_value("Item", self.item_code, PRODUCT_ID_FIELD, medusa_id)
            variants = product.get("variants", [])
            if variants:
                frappe.db.set_value("Item", self.item_code, VARIANT_ID_FIELD, variants[0].get("id"))
            frappe.db.commit()

    @temp_medusa_session
    def _update_product(self, session, base_url):
        medusa_id = self.get_medusa_product_id()
        payload = self._build_product_payload(is_update=True)
        medusa_request(session, base_url, "PUT", f"{API_PRODUCTS}/{medusa_id}", json=payload)

    def _build_product_payload(self, is_update=False) -> dict:
        price = self._get_selling_price()
        payload = {
            "title": self.item.item_name,
            "description": self.item.description or self.item.item_name,
            "status": "published" if not self.item.disabled else "draft",
            "is_giftcard": False,
            "discountable": True,
        }
        if not is_update:
            payload["options"] = [{"title": "Default", "values": ["Default"]}]
            payload["variants"] = [{
                "title": self.item.item_name,
                "sku": self.item.item_code,
                "manage_inventory": True,
                "allow_backorder": False,
                "prices": [{
                    "currency_code": (frappe.db.get_default("currency") or "EUR").lower(),
                    "amount": erpnext_price_to_medusa(price),
                }],
            }]
        if self.item.weight_per_unit:
            payload["weight"] = int(self.item.weight_per_unit * 1000)
        return payload

    def _get_selling_price(self) -> float:
        price_list = self.setting.default_selling_price_list
        if not price_list:
            return 0.0
        price = frappe.db.get_value("Item Price", {"item_code": self.item_code, "price_list": price_list, "selling": 1}, "price_list_rate")
        return price or 0.0


def upload_item_to_medusa(doc, method=None):
    if not is_medusa_enabled():
        return
    setting = frappe.get_cached_doc(SETTING_DOCTYPE)
    if method == "after_insert" and not setting.upload_erpnext_items:
        return
    if method == "on_update" and not setting.sync_item_on_update:
        return
    try:
        exporter = MedusaProductExporter(doc.name)
        exporter.export()
    except Exception as e:
        frappe.log_error(f"Medusa product export failed: {doc.name}", str(e))
