"""Sync ERPNext stock levels to Medusa v2 inventory.

Uses the cross-channel ``controllers/inventory`` helpers — same as
Shopify, Unicommerce and Shopware — so delta detection lives in the
shared ``tabEcommerce Item.inventory_synced_on`` column instead of a
private Redis snapshot. Each Ecommerce Item knows when it was last
pushed; ``get_inventory_levels`` returns only rows whose Bin has been
touched since.
"""
import frappe
from frappe.utils import now

from ecommerce_integrations.controllers.inventory import (
    get_inventory_levels,
    update_inventory_sync_status,
)
from ecommerce_integrations.controllers.scheduling import need_to_run
from ecommerce_integrations.medusa.connection import medusa_request, temp_medusa_session
from ecommerce_integrations.medusa.constants import (
    API_INVENTORY_LEVELS_BATCH,
    SETTING_DOCTYPE,
)
from ecommerce_integrations.medusa.constants import (
    MODULE_NAME as MEDUSA_MODULE,
)
from ecommerce_integrations.medusa.utils import (
    get_medusa_variant_id,
    is_medusa_enabled,
)


def sync_inventory_to_medusa():
    """Push changed stock quantities from ERPNext to Medusa.

    Delta detection is driven by the canonical mapping table:
    ``controllers/inventory.get_inventory_levels`` returns the rows
    where ``Bin.modified > Ecommerce Item.inventory_synced_on``. After
    a successful Medusa write we stamp ``inventory_synced_on`` via
    ``update_inventory_sync_status`` so the next call skips them.
    """
    if not is_medusa_enabled():
        return
    setting = frappe.get_cached_doc(SETTING_DOCTYPE)
    if not setting.sync_inventory:
        return
    if not need_to_run(SETTING_DOCTYPE, "inventory_sync_frequency", "last_inventory_sync"):
        return

    wh_to_location = _get_warehouse_to_location_map(setting)
    if not wh_to_location:
        frappe.log_error(
            "No Medusa stock-location mappings configured (Medusa Setting → Warehouse Mappings).",
            "Medusa Inventory Sync",
        )
        return

    levels = get_inventory_levels(tuple(wh_to_location.keys()), MEDUSA_MODULE)
    if not levels:
        return

    # Filter out rows without a Medusa variant id (template-only rows).
    levels = [d for d in levels if d.variant_id]
    if not levels:
        return

    frappe.logger("medusa").info(
        f"Medusa inventory delta: {len(levels)} variants changed since last sync"
    )

    synced_on = now()
    succeeded = _push_levels(levels, wh_to_location)
    for d in succeeded:
        update_inventory_sync_status(d.ecom_item, time=synced_on)
        frappe.db.commit()


@temp_medusa_session
def _push_levels(session, base_url, levels, wh_to_location):
    """Resolve variant -> inventory_item_id and batch-update Medusa.

    Returns the subset of ``levels`` rows that were successfully pushed
    so the caller can stamp their Ecommerce Item rows.
    """
    # Resolve variant -> inventory_item_id in chunks of 50 (Medusa
    # admin search caps the ``id`` filter list).
    variant_ids = sorted({d.variant_id for d in levels if d.variant_id})
    variant_to_inventory_item: dict[str, str] = {}
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

    updates: list[dict] = []
    pushed: list = []
    for d in levels:
        ii_id = variant_to_inventory_item.get(d.variant_id)
        location_id = wh_to_location.get(d.warehouse)
        if not ii_id or not location_id:
            continue
        qty = max(0, int((d.actual_qty or 0) - (d.reserved_qty or 0)))
        updates.append({
            "inventory_item_id": ii_id,
            "location_id": location_id,
            "stocked_quantity": qty,
        })
        pushed.append(d)

    if not updates:
        return []

    for i in range(0, len(updates), 500):
        chunk = updates[i:i + 500]
        medusa_request(
            session, base_url, "POST", API_INVENTORY_LEVELS_BATCH,
            json={"create": [], "update": chunk, "delete": []},
        )

    return pushed


def _get_warehouse_to_location_map(setting) -> dict[str, str]:
    """Return ``{erpnext_warehouse: medusa_stock_location_id}``.

    Reads ``Medusa Setting.warehouse_mappings`` (per-row mapping) and
    falls back to the legacy single-location field
    (``medusa_stock_location_id`` + ``warehouse``) so installs that
    haven't migrated to the multi-warehouse model still work.
    """
    mapping: dict[str, str] = {}
    if hasattr(setting, "get_warehouse_location_map"):
        try:
            mapping.update(setting.get_warehouse_location_map() or {})
        except Exception:
            pass

    if not mapping and setting.medusa_stock_location_id and setting.warehouse:
        mapping[setting.warehouse] = setting.medusa_stock_location_id

    return mapping


def update_stock_on_stock_entry(doc, method=None):
    """Doc-event hook: kick off an inventory sync if any line item is on Medusa."""
    if not is_medusa_enabled():
        return
    setting = frappe.get_cached_doc(SETTING_DOCTYPE)
    if not setting.sync_inventory:
        return
    for item in doc.items:
        if get_medusa_variant_id(item.item_code):
            frappe.enqueue(
                "ecommerce_integrations.medusa.inventory.sync_inventory_to_medusa",
                queue="default",
                is_async=True,
            )
            break


def update_stock_on_stock_reconciliation(doc, method=None):
    update_stock_on_stock_entry(doc, method)
