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
    get_product_visibilities,
    build_channel_prices,
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
        from ecommerce_integrations.shopware6.validators import ShopwareDataValidator

        # Validate item before upload
        is_valid, errors = ShopwareDataValidator.validate_item_for_export(self.item_code)
        if not is_valid:
            # Log and skip - don't throw error
            for error in errors:
                frappe.logger("shopware6").info(f"Skipping {self.item_code}: {error}")
            return None

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

    def _update_product_visibilities(self, client, product_id: str, new_visibilities: list):
        """
        Update product visibility across sales channels.

        Compares existing visibilities with new ones and:
        - Deletes visibilities for channels no longer needed
        - Creates visibilities for new channels
        - Updates visibility level for existing channels if changed

        Args:
            client: Shopware API client
            product_id: Shopware product ID
            new_visibilities: List of new visibility dicts
        """
        try:
            # Get current visibilities
            response = client.request_get(
                f"product/{product_id}?associations[visibilities]={{}}"
            )
            current_vis = response.get("data", {}).get("visibilities", [])

            # Build lookup of current visibilities
            current_by_channel = {
                v.get("salesChannelId"): {
                    "id": v.get("id"),
                    "visibility": v.get("visibility")
                }
                for v in current_vis
            }

            # Build set of new channel IDs
            new_channel_ids = {v["salesChannelId"] for v in new_visibilities}
            new_by_channel = {v["salesChannelId"]: v["visibility"] for v in new_visibilities}

            # Delete visibilities for channels no longer needed
            for channel_id, vis_data in current_by_channel.items():
                if channel_id not in new_channel_ids:
                    try:
                        client.request_delete(f"product-visibility/{vis_data['id']}")
                    except Exception:
                        pass

            # Create or update visibilities for new channels
            for channel_id, visibility in new_by_channel.items():
                if channel_id in current_by_channel:
                    # Check if visibility level changed
                    if current_by_channel[channel_id]["visibility"] != visibility:
                        try:
                            client.request_patch(
                                f"product-visibility/{current_by_channel[channel_id]['id']}",
                                {"visibility": visibility}
                            )
                        except Exception:
                            pass
                else:
                    # Create new visibility
                    try:
                        client.request_post("product-visibility", {
                            "productId": product_id,
                            "salesChannelId": channel_id,
                            "visibility": visibility
                        })
                    except Exception:
                        pass

        except Exception as e:
            frappe.log_error(
                f"Failed to update visibilities for product {product_id}: {e}",
                "Shopware Multi-Channel Visibility"
            )

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

            # Categories
            category_ids = sync_all_item_categories(client, item.item_code)
            if category_ids:
                payload["categories"] = category_ids

            # Sales channel visibility - Multi-Storefront support
            visibilities = get_product_visibilities(item, self.setting)
            if visibilities:
                payload["visibilities"] = visibilities
            else:
                # Fallback to legacy single-channel mode
                sales_channel_id = get_cached_sales_channel_id(client)
                if sales_channel_id:
                    visibilities = [{
                        "salesChannelId": sales_channel_id,
                        "visibility": 30
                    }]
                    payload["visibilities"] = visibilities

            # Tax
            tax_rate = payload.pop("_tax_rate", 19.0)
            tax_id = get_tax_id_by_rate(client, tax_rate)

            # Build channel-specific prices with Shopware Rules
            # This replaces the single-price logic with multi-channel pricing
            if visibilities and len(self.setting.sales_channels or []) > 0:
                # Multi-channel mode: use build_channel_prices
                channel_prices = build_channel_prices(
                    item=item,
                    visibilities=visibilities,
                    setting=self.setting,
                    client=client,
                    tax_rate=tax_rate,
                    currency_id=currency_id
                )
                payload["prices"] = channel_prices
                # Remove single price, use prices array instead
                payload.pop("price", None)
            else:
                # Single-channel mode: keep original price logic
                if currency_id and payload.get("price"):
                    payload["price"][0]["currencyId"] = currency_id
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
                # For updates with multi-channel support, update visibilities separately
                new_visibilities = payload.pop("visibilities", None)
                client.request_patch(f"product/{product_id}", payload)

                # Update visibilities if multi-channel is configured
                if new_visibilities and len(self.setting.sales_channels or []) > 0:
                    self._update_product_visibilities(client, product_id, new_visibilities)
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
