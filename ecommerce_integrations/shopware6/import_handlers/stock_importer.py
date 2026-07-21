"""
Shopware 6 Stock Importer

Imports stock levels from Shopware to ERPNext.
Creates Stock Entry documents for adjustments.

IMPORTANT: This is disabled by default. Enable via Shopware Settings:
'sync_inventory_from_shopware' = True

Features:
- Import current stock levels from Shopware
- Create Stock Entry (Material Receipt/Issue) for adjustments
- Webhook support for real-time stock updates
- Audit trail via Stock Entry

Usage:
    from ecommerce_integrations.shopware6.import_handlers.stock_importer import (
        import_stock_from_shopware,
        sync_stock_for_product,
    )

    # Sync stock for all products
    import_stock_from_shopware(limit=100)

    # Sync stock for a single product
    sync_stock_for_product(item_code="ITEM-001")
"""

from typing import Any

import frappe
from frappe.utils import flt

from ecommerce_integrations.shopware6.connection import get_shopware_client, temp_shopware_session
from ecommerce_integrations.shopware6.constants import MODULE_NAME, SETTING_DOCTYPE
from ecommerce_integrations.shopware6.utils import get_logger


class StockImporter:
    """
    Imports stock levels from Shopware to ERPNext.

    Stock in Shopware is stored per product with 'stock' and 'availableStock' fields.
    This class syncs these to ERPNext Bin via Stock Entry documents.
    """

    def __init__(self):
        self.setting = frappe.get_cached_doc(SETTING_DOCTYPE)
        self.logger = get_logger("StockImporter")

        # Check if import is enabled (default: False for safety)
        self.import_enabled = getattr(self.setting, 'sync_inventory_from_shopware', False)

        # Target warehouse for imported stock
        self.warehouse = getattr(self.setting, 'warehouse', None)

    def is_import_enabled(self) -> bool:
        """Check if stock import is enabled in settings."""
        return self.import_enabled

    def get_current_erpnext_stock(self, item_code: str) -> float:
        """
        Get current stock level in ERPNext for the configured warehouse.

        Args:
            item_code: ERPNext Item code

        Returns:
            Current stock quantity
        """
        if not self.warehouse:
            return 0.0

        actual_qty = frappe.db.get_value(
            "Bin",
            {"item_code": item_code, "warehouse": self.warehouse},
            "actual_qty"
        ) or 0.0

        return flt(actual_qty)

    def get_shopware_stock(self, client, shopware_id: str) -> dict[str, float]:
        """
        Get stock level from Shopware.

        Args:
            client: Shopware API client
            shopware_id: Shopware product ID

        Returns:
            dict with 'stock' and 'available_stock' values
        """
        try:
            response = client.request_get(f"product/{shopware_id}")
            product = response.data or {}

            return {
                "stock": flt(product.get("stock", 0)),
                "available_stock": flt(product.get("availableStock", 0)),
            }
        except Exception as e:
            self.logger.error(f"Failed to get Shopware stock for {shopware_id}", exception=e)
            return {"stock": 0, "available_stock": 0}

    def create_stock_adjustment(
        self,
        item_code: str,
        qty_difference: float,
        reason: str = "Shopware Stock Sync"
    ) -> str | None:
        """
        Create a Stock Entry to adjust stock levels.

        Args:
            item_code: ERPNext Item code
            qty_difference: Positive for receipt, negative for issue
            reason: Reason for adjustment

        Returns:
            Stock Entry name if created, None otherwise
        """
        if not self.warehouse:
            self.logger.error("No warehouse configured for stock import")
            return None

        if qty_difference == 0:
            return None

        try:
            # Determine stock entry type
            if qty_difference > 0:
                stock_entry_type = "Material Receipt"
                target_warehouse = self.warehouse
                source_warehouse = None
            else:
                stock_entry_type = "Material Issue"
                target_warehouse = None
                source_warehouse = self.warehouse
                qty_difference = abs(qty_difference)

            stock_entry = frappe.get_doc({
                "doctype": "Stock Entry",
                "stock_entry_type": stock_entry_type,
                "posting_date": frappe.utils.today(),
                "posting_time": frappe.utils.nowtime(),
                "remarks": f"{reason} - Synced from Shopware",
                "items": [{
                    "item_code": item_code,
                    "qty": qty_difference,
                    "s_warehouse": source_warehouse,
                    "t_warehouse": target_warehouse,
                }]
            })

            stock_entry.insert(ignore_permissions=True)
            stock_entry.submit()

            self.logger.info(
                f"Created Stock Entry {stock_entry.name} for {item_code}: "
                f"{'+' if stock_entry_type == 'Material Receipt' else '-'}{qty_difference}"
            )

            return stock_entry.name

        except Exception as e:
            self.logger.error(f"Failed to create Stock Entry for {item_code}", exception=e)
            return None

    def sync_product_stock(
        self,
        client,
        item_code: str,
        shopware_id: str,
        dry_run: bool = False
    ) -> dict[str, Any]:
        """
        Sync stock for a single product.

        Compares ERPNext and Shopware stock levels and creates
        adjustment if needed.

        Args:
            client: Shopware API client
            item_code: ERPNext Item code
            shopware_id: Shopware product ID
            dry_run: If True, only report differences

        Returns:
            dict with sync result
        """
        result = {
            "item_code": item_code,
            "erpnext_stock": 0,
            "shopware_stock": 0,
            "difference": 0,
            "adjusted": False,
            "stock_entry": None,
            "error": None,
        }

        try:
            # Get current stocks
            erpnext_stock = self.get_current_erpnext_stock(item_code)
            shopware_data = self.get_shopware_stock(client, shopware_id)
            shopware_stock = shopware_data.get("stock", 0)

            result["erpnext_stock"] = erpnext_stock
            result["shopware_stock"] = shopware_stock
            result["difference"] = shopware_stock - erpnext_stock

            # Only adjust if there's a significant difference
            if abs(result["difference"]) >= 1:  # At least 1 unit difference
                if not dry_run:
                    stock_entry = self.create_stock_adjustment(
                        item_code,
                        result["difference"],
                        reason="Shopware Stock Sync"
                    )
                    if stock_entry:
                        result["adjusted"] = True
                        result["stock_entry"] = stock_entry
                else:
                    result["adjusted"] = False  # Would adjust in non-dry-run

        except Exception as e:
            result["error"] = str(e)[:200]

        return result


@frappe.whitelist()
@temp_shopware_session
def import_stock_from_shopware(
    client,
    limit: int = 100,
    dry_run: bool = False
) -> dict[str, Any]:
    """
    Import stock levels from Shopware for all synced products.

    Args:
        client: Shopware API client (injected by decorator)
        limit: Maximum number of products to process
        dry_run: If True, only report differences without adjusting

    Returns:
        dict with import statistics
    """
    from ecommerce_integrations.shopware6.services.access import require_shopware_admin

    require_shopware_admin()
    importer = StockImporter()

    if not importer.is_import_enabled():
        return {
            "success": False,
            "message": "Stock import is disabled in Shopware Settings. "
                      "Enable 'sync_inventory_from_shopware' to use this feature."
        }

    if not importer.warehouse:
        return {
            "success": False,
            "message": "No warehouse configured in Shopware Settings."
        }

    stats = {
        "processed": 0,
        "adjusted": 0,
        "in_sync": 0,
        "errors": 0,
        "total_difference": 0,
        "details": [],
    }

    # Get all synced items (non-templates)
    ecom_items = frappe.get_all(
        "Ecommerce Item",
        filters={"integration": MODULE_NAME, "has_variants": 0},
        fields=["erpnext_item_code", "integration_item_code"],
        limit=limit
    )

    for ecom_item in ecom_items:
        item_code = ecom_item.erpnext_item_code
        shopware_id = ecom_item.integration_item_code

        result = importer.sync_product_stock(client, item_code, shopware_id, dry_run)

        stats["processed"] += 1
        stats["total_difference"] += abs(result.get("difference", 0))

        if result.get("error"):
            stats["errors"] += 1
        elif result.get("adjusted") or (dry_run and abs(result.get("difference", 0)) >= 1):
            stats["adjusted"] += 1
            stats["details"].append({
                "item_code": item_code,
                "erpnext": result["erpnext_stock"],
                "shopware": result["shopware_stock"],
                "diff": result["difference"],
            })
        else:
            stats["in_sync"] += 1

        # Commit periodically
        if stats["processed"] % 20 == 0:
            frappe.db.commit()

    frappe.db.commit()

    return {
        "success": True,
        "dry_run": dry_run,
        "statistics": stats,
        "message": f"Processed {stats['processed']} items: "
                  f"{stats['adjusted']} adjusted, "
                  f"{stats['in_sync']} in sync, "
                  f"{stats['errors']} errors"
                  + (" (DRY RUN)" if dry_run else "")
    }


@frappe.whitelist()
def sync_stock_for_product(item_code: str, dry_run: bool = False) -> dict[str, Any]:
    """
    Sync stock for a single product from Shopware.

    Args:
        item_code: ERPNext Item code
        dry_run: If True, only report difference

    Returns:
        dict with sync result
    """
    from ecommerce_integrations.shopware6.services.access import require_item_write_permission

    require_item_write_permission(item_code)
    importer = StockImporter()

    if not importer.is_import_enabled():
        return {
            "success": False,
            "message": "Stock import is disabled in Shopware Settings"
        }

    # Get Shopware ID
    from ecommerce_integrations.shopware6.export.utils import get_shopware_document_id
    shopware_id = get_shopware_document_id("Item", item_code)

    if not shopware_id:
        return {
            "success": False,
            "message": f"Item {item_code} is not synced to Shopware"
        }

    client = get_shopware_client()
    result = importer.sync_product_stock(client, item_code, shopware_id, dry_run)

    if result.get("error"):
        return {"success": False, "message": result["error"], "result": result}

    return {
        "success": True,
        "dry_run": dry_run,
        "result": result,
        "message": f"ERPNext: {result['erpnext_stock']}, Shopware: {result['shopware_stock']}, "
                  f"Diff: {result['difference']}"
                  + (f" - Created {result['stock_entry']}" if result.get("stock_entry") else "")
    }


def handle_stock_webhook(payload: dict[str, Any]) -> dict[str, Any]:
    """
    Handle stock update webhook from Shopware.

    Called when product.stock.changed event is received.

    Args:
        payload: Webhook payload containing product ID and new stock

    Returns:
        dict with handling result
    """
    importer = StockImporter()

    if not importer.is_import_enabled():
        return {"success": False, "message": "Stock import disabled"}

    # Extract product ID from webhook
    product_id = (
        payload.get("primaryKey") or
        payload.get("id") or
        payload.get("data", {}).get("productId")
    )

    if not product_id:
        return {"success": False, "message": "No product ID in webhook"}

    # Find ERPNext item
    ecom_item = frappe.db.get_value(
        "Ecommerce Item",
        {"integration": MODULE_NAME, "integration_item_code": product_id},
        ["erpnext_item_code"],
        as_dict=True
    )

    if not ecom_item:
        return {"success": False, "message": f"Product {product_id} not linked to ERPNext"}

    # Sync stock
    return sync_stock_for_product(ecom_item.erpnext_item_code)
