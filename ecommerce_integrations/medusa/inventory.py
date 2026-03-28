"""Sync ERPNext stock levels to Medusa v2 inventory."""
import frappe
from ecommerce_integrations.medusa.connection import medusa_request, temp_medusa_session
from ecommerce_integrations.medusa.constants import API_INVENTORY_LEVELS_BATCH, SETTING_DOCTYPE, VARIANT_ID_FIELD
from ecommerce_integrations.medusa.utils import is_medusa_enabled


def sync_inventory_to_medusa():
    if not is_medusa_enabled():
        return
    setting = frappe.get_cached_doc(SETTING_DOCTYPE)
    if not setting.sync_inventory:
        return
    if not setting.medusa_stock_location_id:
        frappe.log_error("Medusa stock location ID not configured", "Medusa Inventory Sync")
        return
    items = frappe.db.get_all("Item", filters={VARIANT_ID_FIELD: ["is", "set"]}, fields=["item_code", VARIANT_ID_FIELD])
    if not items:
        return
    updates = []
    for item in items:
        stock_qty = _get_stock_qty(item["item_code"], setting.warehouse)
        updates.append({
            "inventory_item_id": item[VARIANT_ID_FIELD],
            "location_id": setting.medusa_stock_location_id,
            "stocked_quantity": max(0, int(stock_qty)),
        })
    if updates:
        _batch_update_inventory(updates)
    frappe.db.set_single_value(SETTING_DOCTYPE, "last_inventory_sync", frappe.utils.now_datetime())
    frappe.db.commit()


@temp_medusa_session
def _batch_update_inventory(session, base_url, updates: list):
    medusa_request(session, base_url, "POST", API_INVENTORY_LEVELS_BATCH, json={"update": updates})


def _get_stock_qty(item_code: str, warehouse: str) -> float:
    if not warehouse:
        return 0.0
    return frappe.db.get_value("Bin", {"item_code": item_code, "warehouse": warehouse}, "actual_qty") or 0.0


def update_stock_on_stock_entry(doc, method=None):
    if not is_medusa_enabled():
        return
    setting = frappe.get_cached_doc(SETTING_DOCTYPE)
    if not setting.sync_inventory:
        return
    for item in doc.items:
        if frappe.db.get_value("Item", item.item_code, VARIANT_ID_FIELD):
            frappe.enqueue("ecommerce_integrations.medusa.inventory.sync_inventory_to_medusa", queue="default", is_async=True)
            break


def update_stock_on_stock_reconciliation(doc, method=None):
    update_stock_on_stock_entry(doc, method)
