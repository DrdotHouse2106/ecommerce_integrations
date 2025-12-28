"""
Shopware Setting Controller Base Class

Extends the ecommerce SettingController with Shopware-specific functionality.
Provides warehouse mapping, tax account mapping, and common settings access.
"""

from typing import Dict, List, Optional

import frappe
from frappe import _

from ecommerce_integrations.controllers.setting import (
    SettingController,
    ERPNextWarehouse,
    IntegrationWarehouse,
)
from ecommerce_integrations.shopware6.constants import SETTING_DOCTYPE


class ShopwareSettingController(SettingController):
    """
    Base controller for Shopware settings.

    Provides standardized interface for:
    - Enabling/disabling integration
    - Warehouse mapping (ERPNext <-> Shopware)
    - Tax account mapping
    - Payment method mapping
    - Common settings access

    Usage:
        setting = get_shopware_setting()
        if setting.is_enabled():
            warehouses = setting.get_erpnext_warehouses()
    """

    def is_enabled(self) -> bool:
        """
        Check if Shopware integration is enabled.

        Returns:
            True if integration is enabled
        """
        return bool(getattr(self, "enable_shopware", False))

    def get_erpnext_warehouses(self) -> List[ERPNextWarehouse]:
        """
        Get list of ERPNext warehouses configured for Shopware sync.

        Returns:
            List of ERPNext warehouse names
        """
        warehouses = []

        # Check warehouse mapping table
        if hasattr(self, "warehouse_mappings"):
            for mapping in self.warehouse_mappings:
                if mapping.erpnext_warehouse:
                    warehouses.append(ERPNextWarehouse(mapping.erpnext_warehouse))

        # Fallback to default warehouse
        if not warehouses and hasattr(self, "warehouse"):
            warehouses.append(ERPNextWarehouse(self.warehouse))

        return warehouses

    def get_erpnext_to_integration_wh_mapping(self) -> Dict[ERPNextWarehouse, IntegrationWarehouse]:
        """
        Get mapping from ERPNext warehouse to Shopware location.

        Returns:
            Dict mapping ERPNext warehouse -> Shopware location ID
        """
        mapping = {}

        if hasattr(self, "warehouse_mappings"):
            for wh_map in self.warehouse_mappings:
                if wh_map.erpnext_warehouse and wh_map.shopware_location_id:
                    mapping[ERPNextWarehouse(wh_map.erpnext_warehouse)] = IntegrationWarehouse(
                        wh_map.shopware_location_id
                    )

        return mapping

    def get_integration_to_erpnext_wh_mapping(self) -> Dict[IntegrationWarehouse, ERPNextWarehouse]:
        """
        Get mapping from Shopware location to ERPNext warehouse.

        Returns:
            Dict mapping Shopware location ID -> ERPNext warehouse
        """
        mapping = {}

        if hasattr(self, "warehouse_mappings"):
            for wh_map in self.warehouse_mappings:
                if wh_map.erpnext_warehouse and wh_map.shopware_location_id:
                    mapping[IntegrationWarehouse(wh_map.shopware_location_id)] = ERPNextWarehouse(
                        wh_map.erpnext_warehouse
                    )

        return mapping

    def get_default_warehouse(self) -> Optional[str]:
        """
        Get the default warehouse for inventory sync.

        Returns:
            Default warehouse name or None
        """
        return getattr(self, "warehouse", None)

    def get_default_company(self) -> Optional[str]:
        """
        Get the default company for transactions.

        Returns:
            Company name or None
        """
        return getattr(self, "company", None)

    def get_cost_center(self) -> Optional[str]:
        """
        Get the default cost center.

        Returns:
            Cost center name or None
        """
        return getattr(self, "cost_center", None)

    def get_cash_bank_account(self) -> Optional[str]:
        """
        Get the cash/bank account for payment entries.

        Returns:
            Account name or None
        """
        return getattr(self, "cash_bank_account", None)

    def get_tax_account_mapping(self) -> Dict[str, str]:
        """
        Get mapping from Shopware tax names to ERPNext tax accounts.

        Returns:
            Dict mapping tax name -> ERPNext tax account
        """
        mapping = {}

        if hasattr(self, "tax_account_mappings"):
            for tax_map in self.tax_account_mappings:
                if tax_map.shopware_tax_name and tax_map.erpnext_tax_account:
                    mapping[tax_map.shopware_tax_name] = tax_map.erpnext_tax_account

        return mapping

    def get_default_tax_account(self) -> Optional[str]:
        """
        Get the default sales tax account.

        Returns:
            Default tax account name or None
        """
        return getattr(self, "default_sales_tax_account", None)

    def get_shipping_charges_account(self) -> Optional[str]:
        """
        Get the account for shipping charges.

        Returns:
            Shipping charges account name or None
        """
        return getattr(self, "default_shipping_charges_account", None)

    def get_shop_url(self) -> str:
        """
        Get the Shopware shop URL (normalized).

        Returns:
            Shop URL without trailing slash
        """
        url = getattr(self, "shop_url", "") or ""
        url = url.rstrip("/")
        if url and not url.startswith("http"):
            url = f"https://{url}"
        return url

    def get_api_url(self) -> str:
        """
        Get the Shopware Admin API URL.

        Returns:
            API URL string
        """
        return f"{self.get_shop_url()}/api"

    def get_service_item(self, service_type: str) -> Optional[str]:
        """
        Get the ERPNext Item code for a service type.

        Args:
            service_type: Type of service ('tel_avis' or 'forklift')

        Returns:
            Item code or None
        """
        field_map = {
            "tel_avis": "tel_avis_item",
            "forklift": "forklift_item",
        }
        field_name = field_map.get(service_type)
        if field_name:
            return getattr(self, field_name, None)
        return None

    def get_service_item_price(self, service_type: str) -> float:
        """
        Get the price for a service item.

        Args:
            service_type: Type of service ('tel_avis' or 'forklift')

        Returns:
            Price as float (0.0 if not configured)
        """
        field_map = {
            "tel_avis": "tel_avis_price",
            "forklift": "forklift_price",
        }
        field_name = field_map.get(service_type)
        if field_name:
            return float(getattr(self, field_name, 0) or 0)
        return 0.0

    def is_bulk_sync_enabled(self) -> bool:
        """
        Check if bulk sync mode is enabled.

        Returns:
            True if bulk sync is enabled
        """
        return bool(getattr(self, "enable_bulk_sync", False))

    def get_bulk_sync_threshold(self) -> int:
        """
        Get the threshold for triggering bulk sync.

        Returns:
            Number of items to trigger bulk mode
        """
        return int(getattr(self, "bulk_sync_threshold", 50) or 50)

    def get_bulk_sync_batch_size(self) -> int:
        """
        Get the batch size for bulk sync processing.

        Returns:
            Batch size
        """
        return int(getattr(self, "bulk_sync_batch_size", 50) or 50)


def get_shopware_setting() -> ShopwareSettingController:
    """
    Get the Shopware Setting document.

    Uses caching for performance.

    Returns:
        ShopwareSetting document
    """
    return frappe.get_cached_doc(SETTING_DOCTYPE)


def get_setting_value(field: str, default: any = None) -> any:
    """
    Get a specific setting value.

    Convenience function for quick access to single settings.

    Args:
        field: Field name to retrieve
        default: Default value if field is empty

    Returns:
        Field value or default
    """
    setting = get_shopware_setting()
    value = getattr(setting, field, None)
    return value if value is not None else default
