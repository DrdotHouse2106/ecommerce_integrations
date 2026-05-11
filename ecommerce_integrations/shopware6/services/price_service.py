"""
Shopware 6 Price Service

Single source of truth for all price-related operations.
Consolidates price retrieval and calculation logic from price_handler.py and product_mapper.py.
"""

from typing import Any

import frappe
from frappe.utils import flt

from ecommerce_integrations.shopware6.constants import ITEM_SELLING_RATE_FIELD, SETTING_DOCTYPE
from ecommerce_integrations.shopware6.utils import get_logger


class PriceService:
    """
    Centralized price service for Shopware integration.

    Provides consistent price retrieval and calculation across all sync operations.

    Usage:
        service = PriceService()

        # Get net price for item
        net_price = service.get_item_net_price("ITEM-001")

        # Get channel-specific price
        price = service.get_channel_price("ITEM-001", sales_channel_doc)

        # Build Shopware price payload
        payload = service.build_price_payload("ITEM-001", currency_id="...")
    """

    def __init__(self, setting=None):
        """
        Initialize the price service.

        Args:
            setting: Optional Shopware Setting document. If not provided, will be loaded.
        """
        self.setting = setting or frappe.get_cached_doc(SETTING_DOCTYPE)
        self.logger = get_logger("PriceService")

        # Cache for tax rates
        self._tax_rate_cache: dict[str, float] = {}

    def get_item_net_price(
        self,
        item_code: str,
        price_list: str | None = None
    ) -> float:
        """
        Get the net selling price for an item.

        Follows this fallback chain:
        1. If price_list specified: look in that price list only
        2. standard_rate field on Item
        3. Any selling price in Item Price table

        Args:
            item_code: ERPNext Item code
            price_list: Optional price list to use

        Returns:
            Net price as float (0.0 if no price found)
        """
        # If price_list is specified, prioritize Item Price table
        if price_list:
            item_price = frappe.db.get_value(
                "Item Price",
                {
                    "item_code": item_code,
                    "price_list": price_list,
                    "selling": 1,
                    "price_list_rate": [">", 0]
                },
                "price_list_rate"
            )
            if item_price:
                return flt(item_price)

        # Try standard_rate as fallback
        item = frappe.get_cached_doc("Item", item_code)
        price = flt(item.get(ITEM_SELLING_RATE_FIELD) or 0)

        if price > 0:
            return price

        # Try Item Price table (any price list)
        item_price = frappe.db.get_value(
            "Item Price",
            {"item_code": item_code, "selling": 1, "price_list_rate": [">", 0]},
            "price_list_rate",
            order_by="price_list_rate desc"
        )

        return flt(item_price) if item_price else 0.0

    def get_channel_price(
        self,
        item_code: str,
        sales_channel
    ) -> float:
        """
        Get the price for an item in a specific sales channel.

        Priority:
        1. Channel has its own price_list -> use that price list
        2. Channel has price_adjustment_percent -> apply to base price
        3. Fallback -> use default_selling_price_list

        Args:
            item_code: ERPNext Item code
            sales_channel: Shopware Sales Channel row from setting

        Returns:
            Price as float
        """
        # Priority 1: Channel has its own price list
        if sales_channel.price_list:
            return self.get_item_net_price(item_code, sales_channel.price_list)

        # Get base price from default price list
        default_price_list = getattr(self.setting, 'default_selling_price_list', None)
        base_price = self.get_item_net_price(item_code, default_price_list)

        # Priority 2: Channel has percentage adjustment
        adjustment_percent = flt(getattr(sales_channel, 'price_adjustment_percent', 0))
        if adjustment_percent != 0:
            adjusted_price = base_price * (1 + adjustment_percent / 100)
            return round(adjusted_price, 2)

        # Priority 3: Return base price as-is
        return base_price

    def get_item_tax_rate(self, item_code: str) -> float:
        """
        Get the tax rate for an item from its tax template.

        Uses caching to avoid repeated database lookups.

        Args:
            item_code: ERPNext Item code

        Returns:
            Tax rate as float (default 19.0 for German VAT)
        """
        # Check cache first
        if item_code in self._tax_rate_cache:
            return self._tax_rate_cache[item_code]

        tax_rate = 19.0  # Default German VAT

        try:
            item = frappe.get_cached_doc("Item", item_code)

            if hasattr(item, 'taxes') and item.taxes:
                for tax_row in item.taxes:
                    if tax_row.item_tax_template:
                        template_rate = frappe.db.get_value(
                            "Item Tax Template Detail",
                            {"parent": tax_row.item_tax_template},
                            "tax_rate"
                        )
                        if template_rate:
                            tax_rate = flt(template_rate)
                            break
        except Exception:
            pass

        # Cache the result
        self._tax_rate_cache[item_code] = tax_rate
        return tax_rate

    def calculate_gross_price(self, net_price: float, tax_rate: float = 19.0) -> float:
        """
        Calculate gross price from net price.

        Formula: gross = net * (1 + tax_rate/100)

        Args:
            net_price: Net price (excluding tax)
            tax_rate: Tax rate as percentage

        Returns:
            Gross price rounded to 2 decimal places
        """
        if net_price <= 0 or tax_rate < 0:
            return net_price
        return round(net_price * (1 + tax_rate / 100), 2)

    def calculate_net_price(self, gross_price: float, tax_rate: float = 19.0) -> float:
        """
        Calculate net price from gross price.

        Formula: net = gross / (1 + tax_rate/100)

        Args:
            gross_price: Gross price (including tax)
            tax_rate: Tax rate as percentage

        Returns:
            Net price rounded to 2 decimal places
        """
        if gross_price <= 0 or tax_rate < 0:
            return gross_price
        return round(gross_price / (1 + tax_rate / 100), 2)

    def validate_price(self, item_code: str, net_price: float) -> tuple[bool, str]:
        """
        Validate a price before syncing to Shopware.

        Args:
            item_code: ERPNext Item code
            net_price: Net price to validate

        Returns:
            Tuple of (is_valid, error_message)
        """
        if net_price <= 0:
            return False, f"Item {item_code} has no valid price (net_price={net_price})"

        if net_price <= 0.02:
            return False, f"Item {item_code} has suspiciously low price ({net_price}€)"

        return True, ""

    def build_price_payload(
        self,
        item_code: str,
        currency_id: str,
        price_list: str | None = None
    ) -> tuple[list[dict[str, Any]] | None, str]:
        """
        Build a complete Shopware price payload for an item.

        Args:
            item_code: ERPNext Item code
            currency_id: Shopware currency ID
            price_list: Optional price list to use

        Returns:
            Tuple of (price_payload, error_message)
            If error, price_payload is None and error_message is set
        """
        net_price = self.get_item_net_price(item_code, price_list)

        # Validate price
        is_valid, error = self.validate_price(item_code, net_price)
        if not is_valid:
            return None, error

        tax_rate = self.get_item_tax_rate(item_code)
        gross_price = self.calculate_gross_price(net_price, tax_rate)

        price_payload = [{
            "currencyId": currency_id,
            "gross": gross_price,
            "net": net_price,
            "linked": False,
        }]

        return price_payload, ""

    def get_price_summary(self, item_code: str, price_list: str | None = None) -> dict[str, Any]:
        """
        Get a complete price summary for an item.

        Useful for debugging and logging.

        Args:
            item_code: ERPNext Item code
            price_list: Optional price list

        Returns:
            Dict with net_price, gross_price, tax_rate, and validation status
        """
        net_price = self.get_item_net_price(item_code, price_list)
        tax_rate = self.get_item_tax_rate(item_code)
        gross_price = self.calculate_gross_price(net_price, tax_rate)
        is_valid, error = self.validate_price(item_code, net_price)

        return {
            "item_code": item_code,
            "price_list": price_list,
            "net_price": net_price,
            "gross_price": gross_price,
            "tax_rate": tax_rate,
            "is_valid": is_valid,
            "error": error if not is_valid else None,
        }

    def clear_cache(self):
        """Clear the internal tax rate cache."""
        self._tax_rate_cache.clear()


# Global service instance for convenience
_price_service_instance: PriceService | None = None


def get_price_service() -> PriceService:
    """
    Get the global PriceService instance.

    Returns:
        PriceService: The global price service instance
    """
    global _price_service_instance
    if _price_service_instance is None:
        _price_service_instance = PriceService()
    return _price_service_instance
