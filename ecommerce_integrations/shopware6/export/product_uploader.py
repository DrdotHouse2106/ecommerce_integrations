"""
Shopware 6 Product Uploader

Main ShopwareProductUploader class for uploading ERPNext Items to Shopware.
Follows the pattern from Shopify integration (ShopifyProduct class).

NOTE: This class was renamed from ShopwareProduct to ShopwareProductUploader
to avoid confusion with the import class in product.py.
"""

from typing import Optional

import frappe
from frappe import _

# Note: get_shopware_client is imported inside upload() method to avoid circular imports
from ecommerce_integrations.shopware6.constants import MODULE_NAME, SETTING_DOCTYPE
from ecommerce_integrations.shopware6.utils import get_logger
from ecommerce_integrations.shopware6.export.utils import generate_uuid, get_shopware_document_id
from ecommerce_integrations.shopware6.export.product_mapper import (
    map_erpnext_item_to_shopware,
    get_tax_id_by_rate,
    get_or_create_manufacturer,
    get_or_create_delivery_time,
    get_cached_currency_id,
    get_cached_sales_channel_id,
    get_product_visibilities,
    build_channel_prices,
)
from ecommerce_integrations.shopware6.export.category_handler import (
    sync_all_item_categories,
    clear_product_categories,
)
from ecommerce_integrations.shopware6.export.property_handler import (
    ensure_shopware_custom_field_set,
    get_item_custom_fields,
    get_item_properties,
    get_or_create_property_group,
    get_or_create_property_option,
    clear_product_properties,
)
from ecommerce_integrations.shopware6.export.image_handler import sync_product_images_to_shopware


class ShopwareProductUploader:
    """
    Shopware product uploader following Shopify integration patterns.

    Handles uploading ERPNext Items to Shopware (ERPNext -> Shopware direction).

    Usage:
        uploader = ShopwareProductUploader(item_code="ITEM-001")

        if not uploader.is_synced():
            uploader.upload()
    """

    def __init__(self, item_code: str, shopware_id: str = None):
        """
        Initialize ShopwareProductUploader.

        Args:
            item_code: ERPNext Item code
            shopware_id: Existing Shopware product ID (optional)
        """
        self.item_code = item_code
        self.shopware_id = shopware_id or get_shopware_document_id("Item", item_code)
        self.setting = frappe.get_cached_doc(SETTING_DOCTYPE)
        self.logger = get_logger("ShopwareProductUploader")

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
        from ecommerce_integrations.shopware6.validators import ERPNextDataValidator

        # Validate item before upload
        is_valid, errors = ERPNextDataValidator.validate_item_for_export(self.item_code)
        if not is_valid:
            # Log and skip - don't throw error
            for error in errors:
                self.logger.info(f"Skipping {self.item_code}: {error}")
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
            self.logger.error(
                f"Failed to update visibilities for product {product_id}",
                exception=e,
                persist=False  # Don't clutter logs with visibility errors
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

            # Categories - use skip_sync=True for fast lookup (categories synced in bulk Phase 1)
            category_ids = sync_all_item_categories(client, item.item_code, skip_sync=True)
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
                # Keep price as default/fallback — Shopware requires it for product creation.
                # Only remove price on updates (handled below in product_exists block).
                if currency_id and payload.get("price"):
                    payload["price"][0]["currencyId"] = currency_id
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

            # Delivery Time
            delivery_time_str = getattr(item, 'delivery_time', None)
            if delivery_time_str:
                delivery_time_id = get_or_create_delivery_time(client, delivery_time_str)
                if delivery_time_id:
                    payload["deliveryTimeId"] = delivery_time_id

            # Check if exists
            product_exists = False
            try:
                client.request_get(f"product/{product_id}")
                product_exists = True
            except BaseException:
                pass

            if product_exists:
                # For updates, remove default price when channel prices are set
                # (channel prices via rules take precedence over default price)
                if payload.get("prices"):
                    payload.pop("price", None)
                # For updates with multi-channel support, update visibilities separately
                new_visibilities = payload.pop("visibilities", None)
                # Store categories before clearing (for debug)
                new_categories = payload.get("categories", [])
                # Clear old categories before setting new ones
                if new_categories:
                    clear_product_categories(client, product_id)
                    frappe.logger().info(f"Cleared categories for {product_id}, will add: {new_categories}")
                # Clear old properties before setting new ones (don't let errors break sync)
                if payload.get("properties"):
                    try:
                        clear_product_properties(client, product_id)
                    except BaseException:
                        pass  # Property clearing is optional, don't break sync
                # CRITICAL: Clear ALL old advanced/rule-based prices before setting new ones
                # This ensures product has ONLY prices from ERPNext
                try:
                    prices_response = client.request_post("search/product-price", {
                        "filter": [{"type": "equals", "field": "productId", "value": product_id}],
                        "limit": 100
                    })
                    for price_entry in prices_response.get("data", []):
                        try:
                            client.request_delete(f"product-price/{price_entry.get('id')}")
                        except Exception:
                            pass
                except Exception:
                    pass
                
                # Log payload for debugging
                logger = get_logger("product_uploader")
                logger.debug(f"PATCH payload for {product_id}: categories={payload.get('categories', [])}")
                
                try:
                    patch_result = client.request_patch(f"product/{product_id}", payload)
                    logger.debug(f"PATCH result for {product_id}: {patch_result if patch_result else 'None'}")
                except Exception as patch_err:
                    logger.error(
                        f"PATCH failed for product {product_id}",
                        exception=patch_err,
                        persist=True,
                        request_data={"payload": payload}
                    )
                    raise

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

            self.logger.success(
                f"Uploaded product: {item.name} -> {product_id}",
                request_data=payload,
            )

            return product_id

        except Exception as e:
            self.logger.error(
                f"Failed to upload {item.name}",
                exception=e,
                rollback=True,
            )
            return None


@frappe.whitelist()
def deactivate_product_in_shopware(item_code: str) -> bool:
    """
    Deactivate a product in Shopware and clean up the Ecommerce Item link.

    Called when an ERPNext Item is deleted (on_trash). Deactivates rather than
    deletes the Shopware product for safety (can be re-activated manually).

    Args:
        item_code: ERPNext Item code

    Returns:
        True if successful or product not in Shopware
    """
    from ecommerce_integrations.shopware6.connection import temp_shopware_session
    from ecommerce_integrations.shopware6.utils import create_shopware_log, require_item_write_permission

    require_item_write_permission(item_code)
    logger = get_logger("deactivate_product_in_shopware")

    # Find the Ecommerce Item linking record
    ecom_item = frappe.db.get_value(
        "Ecommerce Item",
        {"integration": MODULE_NAME, "erpnext_item_code": item_code},
        ["name", "integration_item_code"],
        as_dict=True,
    )

    if not ecom_item:
        logger.info(f"Item '{item_code}' not synced to Shopware, nothing to deactivate")
        return True

    shopware_id = ecom_item.integration_item_code

    # Deactivate in Shopware
    try:
        @temp_shopware_session
        def _deactivate(client):
            client.request_patch(
                f"product/{shopware_id}",
                {"active": False}
            )

        _deactivate()

        logger.info(f"Deactivated Shopware product '{item_code}' (ID: {shopware_id})")
        create_shopware_log(
            status="Success",
            method="deactivate_product_in_shopware",
            message=f"Deactivated product '{item_code}' (ID: {shopware_id})",
            make_new=True,
        )
    except Exception as e:
        logger.error(f"Failed to deactivate Shopware product '{item_code}': {e}")
        create_shopware_log(
            status="Error",
            method="deactivate_product_in_shopware",
            message=f"Failed to deactivate product '{item_code}'",
            exception=str(e),
            make_new=True,
        )
        # Continue to delete the Ecommerce Item even if Shopware call fails
        # (the item is being deleted in ERPNext anyway)

    # Delete the Ecommerce Item linking record
    try:
        frappe.delete_doc("Ecommerce Item", ecom_item.name, ignore_permissions=True)
        logger.info(f"Deleted Ecommerce Item link for '{item_code}'")
    except Exception as e:
        logger.warning(f"Failed to delete Ecommerce Item for '{item_code}': {e}")

    return True


def upload_erpnext_item_to_shopware(item_code: str) -> Optional[str]:
    """
    Convenience function to upload an ERPNext Item to Shopware.

    This is the main entry point for product sync.

    Args:
        item_code: ERPNext Item code

    Returns:
        Shopware product ID if successful, None otherwise
    """
    uploader = ShopwareProductUploader(item_code)
    return uploader.upload()


@frappe.whitelist()
def sync_item_if_changed(item_code: str, force: bool = False) -> dict:
    """
    Idempotent sync: Only update Shopware if ERPNext item has changed.

    Compares current Shopware state with ERPNext state and only performs
    an update if there are actual differences.

    Args:
        item_code: ERPNext Item code
        force: Force sync even if no differences detected

    Returns:
        dict with keys: synced, differences, shopware_id, message
    """
    from ecommerce_integrations.shopware6.connection import get_shopware_client
    from ecommerce_integrations.shopware6.export.reconciliation import compare_item_with_shopware
    from frappe.utils import flt
    from ecommerce_integrations.shopware6.utils import require_item_write_permission

    require_item_write_permission(item_code)
    result = {
        "synced": False,
        "differences": [],
        "shopware_id": None,
        "message": "",
    }

    uploader = ShopwareProductUploader(item_code)

    if not uploader.shopware_id:
        # Not synced yet - do full sync
        result["shopware_id"] = uploader.upload()
        result["synced"] = True
        result["message"] = "New product created"
        return result

    # Fetch current Shopware state
    client = get_shopware_client()
    try:
        response = client.request_get(
            f"product/{uploader.shopware_id}?associations[categories]={{}}&associations[media]={{}}&associations[cover][associations][media]={{}}"
        )
        shopware_data = response.get("data", {})
    except Exception as e:
        # Product doesn't exist in Shopware - re-create
        result["shopware_id"] = uploader.upload()
        result["synced"] = True
        result["message"] = f"Product recreated (was missing in Shopware)"
        return result

    # Compare states
    item = uploader.get_erpnext_item()
    comparison = compare_item_with_shopware(item, shopware_data, compare_categories=True)

    result["differences"] = comparison.get("differences", [])

    if force or comparison.get("needs_sync"):
        result["shopware_id"] = uploader.upload()
        result["synced"] = True
        result["message"] = f"Updated: {', '.join(result['differences'])}" if result["differences"] else "Force sync"
    else:
        result["shopware_id"] = uploader.shopware_id
        result["message"] = "Already in sync"

    return result


def batch_sync_if_changed(item_codes: list, force: bool = False) -> dict:
    """
    Batch idempotent sync for multiple items using Batch API.

    Uses BatchProductUploader for high-performance bulk operations.

    Args:
        item_codes: List of ERPNext Item codes
        force: Force sync even if no differences

    Returns:
        dict with statistics and details
    """
    from ecommerce_integrations.shopware6.connection import get_shopware_client
    from ecommerce_integrations.shopware6.export.batch_uploader import BatchProductUploader
    from ecommerce_integrations.shopware6.export.reconciliation import (
        get_shopware_products_batch,
        compare_item_with_shopware,
    )
    from ecommerce_integrations.shopware6.export.utils import get_shopware_document_id

    stats = {
        "total": len(item_codes),
        "in_sync": 0,
        "synced": 0,
        "created": 0,
        "failed": 0,
        "details": [],
    }

    if not item_codes:
        return stats

    # Split into existing and new items, and by type (template/variant/simple)
    existing_items = []
    new_items = []

    for item_code in item_codes:
        shopware_id = get_shopware_document_id("Item", item_code)
        if shopware_id:
            existing_items.append({"item_code": item_code, "shopware_id": shopware_id})
        else:
            new_items.append(item_code)

    # Items that need sync
    items_to_sync = []

    # If force mode, sync all existing items
    if force:
        items_to_sync = [item["item_code"] for item in existing_items]
    else:
        # Fetch current state for existing items in batch and compare
        client = get_shopware_client()
        shopware_ids = [item["shopware_id"] for item in existing_items]
        shopware_products = get_shopware_products_batch(client, shopware_ids, include_categories=True)

        for item_data in existing_items:
            item_code = item_data["item_code"]
            shopware_id = item_data["shopware_id"]

            try:
                item = frappe.get_doc("Item", item_code)
                shopware_data = shopware_products.get(shopware_id)

                if not shopware_data:
                    # Product missing in Shopware - needs sync
                    items_to_sync.append(item_code)
                    continue

                comparison = compare_item_with_shopware(item, shopware_data, compare_categories=True)

                if comparison.get("needs_sync"):
                    items_to_sync.append(item_code)
                    stats["details"].append({
                        "item_code": item_code,
                        "action": "needs_update",
                        "differences": comparison.get("differences", [])
                    })
                else:
                    stats["in_sync"] += 1

            except Exception as e:
                stats["failed"] += 1
                stats["details"].append({"item_code": item_code, "action": "error", "error": str(e)[:100]})

    # Combine items to sync (existing that need update + new items)
    all_items_to_sync = items_to_sync + new_items

    if not all_items_to_sync:
        return stats

    # Categorize items by type for BatchProductUploader
    templates = []
    variants = []
    simple_items = []

    for item_code in all_items_to_sync:
        try:
            item = frappe.get_cached_doc("Item", item_code)
            if item.has_variants:
                templates.append(item_code)
            elif item.variant_of:
                variants.append(item_code)
            else:
                simple_items.append(item_code)
        except Exception:
            simple_items.append(item_code)  # Fallback

    # Use BatchProductUploader for bulk operations
    uploader = BatchProductUploader()

    # Sync templates first
    if templates:
        result = uploader.upload_templates(templates, skip_images=True)
        stats["synced"] += result.success
        stats["created"] += len(result.created_ids)
        stats["failed"] += result.failed
        for err in result.errors:
            stats["details"].append({"item_code": err.get("item_code", "?"), "action": "error", "error": err.get("error", "")[:100]})

    # Then simple items
    if simple_items:
        result = uploader.upload_items(simple_items, skip_images=True)
        stats["synced"] += result.success
        stats["created"] += len(result.created_ids)
        stats["failed"] += result.failed
        for err in result.errors:
            stats["details"].append({"item_code": err.get("item_code", "?"), "action": "error", "error": err.get("error", "")[:100]})

    # Finally variants
    if variants:
        result = uploader.upload_variants(variants, skip_images=True)
        stats["synced"] += result.success
        stats["created"] += len(result.created_ids)
        stats["failed"] += result.failed
        for err in result.errors:
            stats["details"].append({"item_code": err.get("item_code", "?"), "action": "error", "error": err.get("error", "")[:100]})

    return stats
