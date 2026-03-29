"""Sync ERPNext stock levels to Medusa v2 inventory."""
import frappe
from ecommerce_integrations.medusa.connection import medusa_request, medusa_request_all, optional_session, temp_medusa_session
from ecommerce_integrations.medusa.constants import (
    API_INVENTORY_ITEMS, API_INVENTORY_LEVELS_BATCH, SETTING_DOCTYPE, VARIANT_ID_FIELD,
)
from ecommerce_integrations.medusa.utils import is_medusa_enabled


def sync_inventory_to_medusa():
    """Sync stock quantities from ERPNext to Medusa for all managed variants.

    Resolves Variant → Inventory Item links via the Medusa API, then
    batch-updates inventory levels at the configured stock location.
    """
    if not is_medusa_enabled():
        return
    setting = frappe.get_cached_doc(SETTING_DOCTYPE)
    if not setting.sync_inventory:
        return
    if not setting.medusa_stock_location_id:
        frappe.log_error("Medusa stock location ID not configured", "Medusa Inventory Sync")
        return

    # Get all ERPNext items that have a medusa_variant_id
    items = frappe.db.get_all(
        "Item",
        filters={VARIANT_ID_FIELD: ["is", "set"], "disabled": 0},
        fields=["item_code", VARIANT_ID_FIELD],
    )
    if not items:
        return

    _sync_inventory_inner(items, setting)


@temp_medusa_session
def _sync_inventory_inner(session, base_url, items, setting):
    """Resolve inventory item IDs and batch-update stock levels."""
    # Build {variant_id: item_code} map from ERPNext
    variant_to_item = {item[VARIANT_ID_FIELD]: item["item_code"] for item in items}

    # Fetch all inventory items with their variant links from Medusa
    inventory_items = medusa_request_all(
        session, base_url, API_INVENTORY_ITEMS,
        "inventory_items",
        params={"fields": "*variants"},
    )

    # Build {inventory_item_id: item_code} by matching via variant link
    updates = []
    for ii in inventory_items:
        ii_id = ii.get("id")
        if not ii_id:
            continue
        # Find which ERPNext item this inventory item belongs to
        for variant in ii.get("variants", []):
            variant_id = variant.get("id")
            item_code = variant_to_item.get(variant_id)
            if item_code:
                stock_qty = _get_stock_qty(item_code, setting.warehouse)
                updates.append({
                    "inventory_item_id": ii_id,
                    "location_id": setting.medusa_stock_location_id,
                    "stocked_quantity": max(0, int(stock_qty)),
                })
                break  # One inventory item per variant

    if updates:
        medusa_request(session, base_url, "POST", API_INVENTORY_LEVELS_BATCH, json={"create": [], "update": updates, "delete": []})

    frappe.db.set_single_value(SETTING_DOCTYPE, "last_inventory_sync", frappe.utils.now_datetime())
    frappe.db.commit()


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
