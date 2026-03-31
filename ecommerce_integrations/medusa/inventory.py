"""Sync ERPNext stock levels to Medusa v2 inventory."""
import frappe
from ecommerce_integrations.controllers.scheduling import need_to_run
from ecommerce_integrations.medusa.connection import medusa_request, temp_medusa_session
from ecommerce_integrations.medusa.constants import (
    API_INVENTORY_LEVELS_BATCH, SETTING_DOCTYPE, VARIANT_ID_FIELD,
)
from ecommerce_integrations.medusa.utils import is_medusa_enabled

INVENTORY_CACHE_KEY = "medusa_inventory_snapshot"


def sync_inventory_to_medusa():
    """Sync stock quantities from ERPNext to Medusa for changed items only.

    Uses delta detection: compares current stock against a cached snapshot
    and only sends items whose quantity changed. Falls back to full sync
    when no snapshot exists (first run or after cache expiry).
    """
    if not is_medusa_enabled():
        return
    setting = frappe.get_cached_doc(SETTING_DOCTYPE)
    if not setting.sync_inventory:
        return
    if not setting.medusa_stock_location_id:
        frappe.log_error("Medusa stock location ID not configured", "Medusa Inventory Sync")
        return
    if not need_to_run(SETTING_DOCTYPE, "inventory_sync_frequency", "last_inventory_sync"):
        return

    # Get all ERPNext items that have a medusa_variant_id
    items = frappe.db.get_all(
        "Item",
        filters={VARIANT_ID_FIELD: ["is", "set"], "disabled": 0},
        fields=["item_code", VARIANT_ID_FIELD],
    )
    if not items:
        return

    # Batch-fetch stock quantities (1 SQL query instead of N)
    item_codes = [i.item_code for i in items]
    stock_map = _batch_get_stock_qty(item_codes, setting.warehouse)

    # Build current snapshot and detect delta
    previous_snapshot = frappe.cache.get_value(INVENTORY_CACHE_KEY) or {}
    current_snapshot = {}
    changed_items = []

    for item in items:
        qty = max(0, int(stock_map.get(item.item_code, 0)))
        current_snapshot[item.item_code] = {
            "variant_id": item[VARIANT_ID_FIELD],
            "qty": qty,
        }
        prev = previous_snapshot.get(item.item_code)
        if not prev or prev.get("qty") != qty:
            changed_items.append(item)

    if not changed_items:
        return

    frappe.logger("medusa").info(
        f"Inventory delta sync: {len(changed_items)} of {len(items)} items changed"
    )

    # Resolve variant_id → inventory_item_id via Medusa API (only for changed items)
    variant_ids = [i[VARIANT_ID_FIELD] for i in changed_items]
    variant_to_qty = {i[VARIANT_ID_FIELD]: current_snapshot[i.item_code]["qty"] for i in changed_items}

    _sync_changed_inventory(variant_ids, variant_to_qty, setting)

    # Save snapshot on success
    frappe.cache.set_value(INVENTORY_CACHE_KEY, current_snapshot, expires_in_sec=86400)


@temp_medusa_session
def _sync_changed_inventory(session, base_url, variant_ids, variant_to_qty, setting):
    """Resolve inventory item IDs for changed variants and batch-update levels."""
    # Fetch variant → inventory_item mapping only for changed variants
    # Process in chunks of 50 to avoid oversized query strings
    variant_to_inventory_item = {}
    for i in range(0, len(variant_ids), 50):
        chunk = variant_ids[i:i + 50]
        result = medusa_request(
            session, base_url, "GET", "/admin/product-variants",
            params={"id": chunk, "fields": "+inventory_items", "limit": 50},
        )
        for v in result.get("variants", []):
            for link in v.get("inventory_items", []):
                ii_id = link.get("inventory_item_id") or link.get("id")
                if ii_id:
                    variant_to_inventory_item[v["id"]] = ii_id
                    break

    # Build batch update payload
    updates = []
    for variant_id, qty in variant_to_qty.items():
        ii_id = variant_to_inventory_item.get(variant_id)
        if ii_id:
            updates.append({
                "inventory_item_id": ii_id,
                "location_id": setting.medusa_stock_location_id,
                "stocked_quantity": qty,
            })

    if not updates:
        return

    # Send in chunks of 500
    for i in range(0, len(updates), 500):
        chunk = updates[i:i + 500]
        medusa_request(
            session, base_url, "POST", API_INVENTORY_LEVELS_BATCH,
            json={"create": [], "update": chunk, "delete": []},
        )


def _batch_get_stock_qty(item_codes: list, warehouse: str = None) -> dict:
    """Fetch stock quantities for multiple items. Chunked to avoid SQL placeholder limits."""
    if not item_codes:
        return {}
    result = {}
    chunk_size = 5000
    for i in range(0, len(item_codes), chunk_size):
        chunk = item_codes[i:i + chunk_size]
        placeholders = ", ".join(["%s"] * len(chunk))
        if warehouse:
            rows = frappe.db.sql(
                f"""SELECT item_code, SUM(actual_qty) as qty
                FROM `tabBin`
                WHERE item_code IN ({placeholders}) AND warehouse = %s
                GROUP BY item_code""",
                (*chunk, warehouse),
                as_dict=True,
            )
        else:
            rows = frappe.db.sql(
                f"""SELECT item_code, SUM(actual_qty) as qty
                FROM `tabBin`
                WHERE item_code IN ({placeholders})
                GROUP BY item_code""",
                chunk,
                as_dict=True,
            )
        for r in rows:
            result[r.item_code] = r.qty
    return result


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
