"""
Shopware 6 Product Uploader

Main ShopwareProduct class for uploading ERPNext Items to Shopware.
Follows the pattern from Shopify integration (ShopifyProduct class).
"""

from typing import Optional

import frappe
from frappe import _

# Note: get_shopware_client is imported inside upload() method to avoid circular imports
from ecommerce_integrations.shopware6.constants import MODULE_NAME, SETTING_DOCTYPE
from ecommerce_integrations.shopware6.utils import create_shopware_log
from ecommerce_integrations.shopware6.export.utils import generate_uuid, get_shopware_document_id
from ecommerce_integrations.shopware6.export.product_mapper import (
    map_erpnext_item_to_shopware,
    get_tax_id_by_rate,
    get_or_create_manufacturer,
    get_cached_currency_id,
    get_cached_sales_channel_id,
)
from ecommerce_integrations.shopware6.export.category_handler import sync_all_item_categories
from ecommerce_integrations.shopware6.export.property_handler import (
    ensure_shopware_custom_field_set,
    get_item_custom_fields,
    get_item_properties,
    get_or_create_property_group,
    get_or_create_property_option,
)
from ecommerce_integrations.shopware6.export.image_handler import sync_product_images_to_shopware


class ShopwareProduct:
    """
    Shopware product handler following Shopify integration patterns.

    Encapsulates all product upload logic into a reusable class.

    Usage:
        product = ShopwareProduct(item_code="ITEM-001")

        if not product.is_synced():
            product.upload()
    """

    def __init__(self, item_code: str, shopware_id: str = None):
        """
        Initialize ShopwareProduct.

        Args:
            item_code: ERPNext Item code
            shopware_id: Existing Shopware product ID (optional)
        """
        self.item_code = item_code
        self.shopware_id = shopware_id or get_shopware_document_id("Item", item_code)
        self.setting = frappe.get_cached_doc(SETTING_DOCTYPE)

        if not self.setting.is_enabled():
            frappe.throw(_("Shopware integration is not enabled."))

    def is_synced(self) -> bool:
        """Check if product is already synced to Shopware."""
        return bool(self.shopware_id)

    def get_erpnext_item(self):
        """Get the ERPNext Item document."""
        return frappe.get_doc("Item", self.item_code)

    def upload(self) -> Optional[str]:
        """
        Upload or update product in Shopware.

        Returns:
            Shopware product ID if successful, None otherwise
        """
        from ecommerce_integrations.shopware6.connection import get_shopware_client

        client = get_shopware_client()
        item = self.get_erpnext_item()

        # Check if template item
        if item.has_variants:
            from ecommerce_integrations.shopware6.export.template_handler import (
                upload_template_item_to_shopware
            )
            return upload_template_item_to_shopware(client, item)

        # Check if variant item
        if item.variant_of:
            from ecommerce_integrations.shopware6.export.variant_handler import (
                upload_variant_item_to_shopware
            )
            return upload_variant_item_to_shopware(client, item)

        # Simple product
        return self._upload_simple_product(client, item)

    def _upload_simple_product(self, client, item) -> Optional[str]:
        """
        Upload a simple (non-variant) product.

        Args:
            client: Shopware API client
            item: ERPNext Item document

        Returns:
            Shopware product ID
        """
        try:
            # Build payload
            payload = map_erpnext_item_to_shopware(item)
            product_id = self.shopware_id or generate_uuid(f"product_{item.item_code}")
            payload["id"] = product_id

            # Currency
            currency_id = get_cached_currency_id(client, "EUR")
            if currency_id and payload.get("price"):
                payload["price"][0]["currencyId"] = currency_id

            # Categories
            category_ids = sync_all_item_categories(client, item.item_code)
            if category_ids:
                payload["categories"] = category_ids

            # Sales channel visibility
            sales_channel_id = get_cached_sales_channel_id(client)
            if sales_channel_id:
                payload["visibilities"] = [{
                    "salesChannelId": sales_channel_id,
                    "visibility": 30
                }]

            # Tax
            tax_rate = payload.pop("_tax_rate", 19.0)
            tax_id = get_tax_id_by_rate(client, tax_rate)
            if tax_id:
                payload["taxId"] = tax_id

            # Manufacturer
            manufacturer_name = (
                getattr(item, 'brand', None) or
                getattr(item, 'default_item_manufacturer', None)
            )
            if manufacturer_name:
                manufacturer_id = get_or_create_manufacturer(client, manufacturer_name)
                if manufacturer_id:
                    payload["manufacturerId"] = manufacturer_id

            # Custom fields
            custom_fields = get_item_custom_fields(item)
            if custom_fields:
                ensure_shopware_custom_field_set(client)
                payload["customFields"] = custom_fields

            # Properties
            properties = get_item_properties(item)
            if properties:
                property_ids = []
                for prop in properties:
                    group_id = get_or_create_property_group(client, prop["group_name"])
                    if group_id:
                        option_id = get_or_create_property_option(
                            client, group_id, prop["group_name"], prop["option_value"]
                        )
                        if option_id:
                            property_ids.append({"id": option_id})

                if property_ids:
                    payload["properties"] = property_ids

            # Check if exists
            product_exists = False
            try:
                client.request_get(f"product/{product_id}")
                product_exists = True
            except BaseException:
                pass

            if product_exists:
                payload.pop("visibilities", None)
                client.request_patch(f"product/{product_id}", payload)
            else:
                client.request_post("product", payload)

            # Create Ecommerce Item link if not exists
            if not self.shopware_id:
                frappe.get_doc({
                    "doctype": "Ecommerce Item",
                    "integration": MODULE_NAME,
                    "erpnext_item_code": item.name,
                    "integration_item_code": product_id,
                    "has_variants": 0,
                    "sku": item.item_code,
                }).insert(ignore_permissions=True)

            self.shopware_id = product_id

            # Sync images
            if getattr(self.setting, 'sync_images_to_shopware', True):
                sync_product_images_to_shopware(client, item, product_id)

            create_shopware_log(
                status="Success",
                request_data=payload,
                message=f"Uploaded product: {item.name} -> {product_id}",
                method="ShopwareProduct.upload"
            )

            return product_id

        except Exception as e:
            create_shopware_log(
                status="Error",
                message=f"Failed to upload {item.name}: {str(e)}",
                exception=str(e),
                method="ShopwareProduct.upload",
                rollback=True
            )
            frappe.log_error(f"Shopware product upload failed for {item.name}: {e}")
            return None


def upload_erpnext_item_to_shopware(item_code: str) -> Optional[str]:
    """
    Convenience function to upload an ERPNext Item to Shopware.

    This is the main entry point for product sync.

    Args:
        item_code: ERPNext Item code

    Returns:
        Shopware product ID if successful, None otherwise
    """
    product = ShopwareProduct(item_code)
    return product.upload()
