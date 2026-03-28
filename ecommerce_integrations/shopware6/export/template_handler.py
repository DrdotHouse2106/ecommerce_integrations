"""
Shopware 6 Template Handler

Handles template/parent product upload for products with variants.
Creates the parent product with configuratorSettings for variant options.
"""

from typing import Optional, Dict, Any, List

import frappe

from ecommerce_integrations.shopware6.constants import MODULE_NAME, SETTING_DOCTYPE
from ecommerce_integrations.shopware6.utils import create_shopware_log, get_logger
from ecommerce_integrations.shopware6.export.utils import generate_uuid, get_shopware_document_id
from ecommerce_integrations.shopware6.export.product_mapper import (
    map_erpnext_item_to_shopware,
    get_tax_id_by_rate,
    get_or_create_manufacturer,
    get_or_create_delivery_time,
    get_cached_currency_id,
    get_cached_sales_channel_id,
    get_actual_variant_values,
    get_product_visibilities,
    build_channel_prices,
)
from ecommerce_integrations.shopware6.export.category_handler import (
    sync_all_item_categories,
    sync_category_hierarchy,
    clear_product_categories,
)
from ecommerce_integrations.shopware6.export.property_handler import (
    get_or_create_property_group,
    get_or_create_variant_option,
    get_or_create_property_option,
    get_template_item_attributes,
    get_item_properties,
    clear_product_properties,
)
from ecommerce_integrations.shopware6.export.image_handler import sync_product_images_to_shopware
from lib_shopware6_api_base import HEADER_index_asynchronously


def _delete_and_recreate_product(client, product_id: str, product_payload: dict, item_code: str) -> None:
    """
    Delete a corrupted product in Shopware and recreate it.

    This is a workaround for the Shopware VariantListingUpdater bug that causes
    "Cardinality violation: 1242 Subquery returns more than 1 row" errors
    when updating products with corrupted variant option data.

    Args:
        client: Shopware API client
        product_id: Shopware product UUID to delete and recreate
        product_payload: The product payload to create
        item_code: ERPNext item code for logging
    """
    import time

    # Step 1: Delete all variants first (required before deleting parent)
    try:
        variants_response = client.request_post("search/product", {
            "filter": [{"type": "equals", "field": "parentId", "value": product_id}],
            "limit": 500
        })
        variants = variants_response.get("data", [])
        for variant in variants:
            try:
                client.request_delete(f"product/{variant['id']}")
            except Exception:
                pass
        if variants:
            frappe.logger().info(f"Deleted {len(variants)} variants of {item_code}")
    except Exception as e:
        frappe.logger().warning(f"Could not delete variants for {item_code}: {e}")

    # Step 2: Delete the parent product
    try:
        client.request_delete(f"product/{product_id}")
        frappe.logger().info(f"Deleted corrupted product {item_code} ({product_id})")
    except Exception as e:
        frappe.logger().error(f"Could not delete product {item_code}: {e}")
        raise

    # Step 3: Wait a moment for Shopware to process
    time.sleep(0.5)

    # Step 4: Recreate the product with visibilities
    # Make sure visibilities are included for new product
    if "visibilities" not in product_payload:
        sales_channel_id = get_cached_sales_channel_id(client)
        if sales_channel_id:
            product_payload["visibilities"] = [{
                "salesChannelId": sales_channel_id,
                "visibility": 30
            }]

    try:
        client.request_post("product", product_payload)
        frappe.logger().info(f"Recreated product {item_code} ({product_id})")
    except Exception as e:
        frappe.logger().error(f"Could not recreate product {item_code}: {e}")
        raise


def clear_product_configurator_settings(client, product_id: str) -> bool:
    """
    Clear all configuratorSettings from a template product in Shopware.

    This ensures that when variant options are updated, old/stale options
    are removed before new ones are set.

    Args:
        client: Shopware API client
        product_id: Shopware product UUID

    Returns:
        True if successful
    """
    import time
    
    try:
        response = client.request_get(
            f"product/{product_id}?associations[configuratorSettings][]"
        )
        product_data = response.get("data", {})
        configurator_settings = product_data.get("configuratorSettings", [])

        if not configurator_settings:
            return True

        # Delete each configurator setting with retry logic
        deleted_count = 0
        error_count = 0
        
        for setting in configurator_settings:
            setting_id = setting.get("id")
            if not setting_id:
                continue
                
            max_retries = 3
            retry_delay = 0.5  # seconds
            
            for attempt in range(max_retries):
                try:
                    client.request_delete(
                        f"product/{product_id}/configurator-settings/{setting_id}"
                    )
                    deleted_count += 1
                    break  # Success, exit retry loop
                    
                except BaseException as e:
                    error_str = str(e)
                    
                    # Ignore 404 errors - setting already deleted/doesn't exist
                    if "404" in error_str or "Not Found" in error_str:
                        frappe.logger().debug(
                            f"ConfiguratorSetting {setting_id} already deleted (404)"
                        )
                        deleted_count += 1
                        break
                    
                    # Handle 500 errors with retry
                    elif "500" in error_str or "Internal Server Error" in error_str:
                        if attempt < max_retries - 1:
                            frappe.logger().warning(
                                f"500 error deleting configuratorSetting {setting_id}, "
                                f"retrying ({attempt + 1}/{max_retries})..."
                            )
                            time.sleep(retry_delay)
                            retry_delay *= 2  # Exponential backoff
                            continue
                        else:
                            get_logger().error(
                                f"Failed to delete configuratorSetting {setting_id} after {max_retries} attempts: {e}",
                                persist=False
                            )
                            error_count += 1
                            break
                    
                    # Other errors
                    else:
                        get_logger().error(
                            f"Error deleting configuratorSetting {setting_id}: {e}",
                            persist=False
                        )
                        error_count += 1
                        break

        frappe.logger().info(
            f"Cleared {deleted_count}/{len(configurator_settings)} configuratorSettings from {product_id} "
            f"({error_count} errors)"
        )
        return error_count == 0

    except Exception as e:
        get_logger().error(f"Error clearing configuratorSettings: {e}", persist=False)
        return False


def upload_template_item_to_shopware(client, template_item) -> Optional[str]:
    """
    Export an ERPNext template item (has_variants=1) to Shopware as a parent product.

    Creates the parent product with configuratorSettings for all variant options.
    Updates existing products including categories.

    Args:
        client: Shopware API client
        template_item: ERPNext Item document with has_variants=1

    Returns:
        Shopware product ID if successful, None otherwise
    """
    setting = frappe.get_cached_doc(SETTING_DOCTYPE)

    # Check if already synced - but continue to update categories
    existing_shopware_id = get_shopware_document_id("Item", template_item.name)

    try:
        # Build base payload
        product_payload = map_erpnext_item_to_shopware(template_item)
        product_id = existing_shopware_id or generate_uuid(f"product_{template_item.item_code}")
        product_payload["id"] = product_id

        # Currency
        currency_id = get_cached_currency_id(client, "EUR")

        # Categories - use skip_sync=True for fast lookup (categories synced in bulk Phase 1)
        category_ids = sync_all_item_categories(client, template_item.item_code, skip_sync=True)
        if category_ids:
            product_payload["categories"] = category_ids
        elif template_item.item_group:
            category_id = sync_category_hierarchy(client, template_item.item_group)
            if category_id:
                product_payload["categories"] = [{"id": category_id}]

        # Sales channel visibility - Multi-Storefront support
        visibilities = get_product_visibilities(template_item, setting)
        if visibilities:
            product_payload["visibilities"] = visibilities
        else:
            # Fallback to legacy single-channel mode
            sales_channel_id = get_cached_sales_channel_id(client)
            if sales_channel_id:
                visibilities = [{
                    "salesChannelId": sales_channel_id,
                    "visibility": 30
                }]
                product_payload["visibilities"] = visibilities

        # Tax
        tax_rate = product_payload.pop("_tax_rate", 19.0)
        tax_id = get_tax_id_by_rate(client, tax_rate)
        if tax_id:
            product_payload["taxId"] = tax_id

        # IMPORTANT: Parent/Template products should NOT have prices
        # Only variants should have prices. Setting price to 0 and removing all advanced prices.
        # This prevents the 0.01 cent price issue in listings and cart.
        product_payload["price"] = [{
            "currencyId": currency_id,
            "gross": 0,
            "net": 0,
            "linked": False,
        }]
        # Explicitly remove any prices array (advanced/rule-based prices)
        product_payload.pop("prices", None)

        # Manufacturer
        manufacturer_name = (
            getattr(template_item, 'brand', None) or
            getattr(template_item, 'default_item_manufacturer', None)
        )
        if manufacturer_name:
            manufacturer_id = get_or_create_manufacturer(client, manufacturer_name)
            if manufacturer_id:
                product_payload["manufacturerId"] = manufacturer_id

        # Build configuratorSettings from Item Attributes
        configurator_settings = []
        attributes = get_template_item_attributes(template_item)
        seen_option_ids = set()

        for attr in attributes:
            attr_name = attr["name"]
            actual_values = get_actual_variant_values(template_item.name, attr_name)

            if not actual_values:
                continue

            group_id = get_or_create_property_group(client, attr_name)
            if not group_id:
                continue

            for value in actual_values:
                option_id = get_or_create_variant_option(client, group_id, attr_name, value)
                if option_id and option_id not in seen_option_ids:
                    seen_option_ids.add(option_id)
                    config_setting_id = generate_uuid(
                        f"config_{template_item.item_code}_{attr_name}_{value}"
                    )
                    configurator_settings.append({
                        "id": config_setting_id,
                        "optionId": option_id,
                    })

        if configurator_settings:
            product_payload["configuratorSettings"] = configurator_settings

        # Build variantListingConfig to show all variants individually in product listings
        # This configures "Expand property values in product listings" with all properties
        # Each variant will get its own displayGroup and appear separately
        # PERFORMANCE FIX: Limit expressionForListings to max 3 attributes to avoid
        # Shopware's O(n^k) display_group query explosion with many property groups
        MAX_EXPRESSION_FOR_LISTINGS = 3
        if attributes:
            configurator_group_config = []
            for idx, attr in enumerate(attributes):
                attr_name = attr["name"]
                group_id = get_or_create_property_group(client, attr_name)
                if group_id:
                    # Only first MAX_EXPRESSION_FOR_LISTINGS attributes get expressionForListings=True
                    # This prevents the display_group indexer query from exploding with 10+ JOINs
                    configurator_group_config.append({
                        "id": group_id,
                        "expressionForListings": idx < MAX_EXPRESSION_FOR_LISTINGS,
                        "representation": "box"
                    })

            if configurator_group_config:
                product_payload["variantListingConfig"] = {
                    "displayParent": False,  # Don't show parent in listing
                    "mainVariantId": None,   # No specific main variant
                    "configuratorGroupConfig": configurator_group_config
                }

        # Properties (filter properties like Material, Farbe etc.)
        properties = get_item_properties(template_item)
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
                product_payload["properties"] = property_ids

        # Delivery Time
        delivery_time_str = getattr(template_item, 'delivery_time', None)
        if delivery_time_str:
            delivery_time_id = get_or_create_delivery_time(client, delivery_time_str)
            if delivery_time_id:
                product_payload["deliveryTimeId"] = delivery_time_id

        # Check if product exists (use existing_shopware_id if available to avoid API call)
        product_exists = bool(existing_shopware_id)
        if not product_exists:
            try:
                client.request_get(f"product/{product_id}")
                product_exists = True
            except BaseException:
                pass

        if product_exists:
            # Pop visibilities for update (handled separately if needed)
            product_payload.pop("visibilities", None)
            # Clear old categories before setting new ones
            if product_payload.get("categories"):
                clear_product_categories(client, product_id)
            # Clear old configuratorSettings before setting new ones
            if product_payload.get("configuratorSettings"):
                clear_product_configurator_settings(client, product_id)
            # Clear old properties before setting new ones
            if product_payload.get("properties"):
                clear_product_properties(client, product_id)
            # CRITICAL: Clear ALL advanced/rule-based prices for parent products
            # Parent products should not have any prices (only variants should)
            try:
                prices_response = client.request_post("search/product-price", {
                    "filter": [{"type": "equals", "field": "productId", "value": product_id}],
                    "limit": 100
                })
                deleted_prices = 0
                for price_entry in prices_response.get("data", []):
                    try:
                        client.request_delete(f"product-price/{price_entry.get('id')}")
                        deleted_prices += 1
                    except Exception:
                        pass
                if deleted_prices > 0:
                    frappe.logger().info(f"Deleted {deleted_prices} advanced prices from parent product {product_id}")
            except Exception as e:
                frappe.logger().warning(f"Could not clear advanced prices for {product_id}: {e}")
            # Update product
            try:
                client.request_patch(f"product/{product_id}", product_payload)
                frappe.logger().info(f"Template {template_item.item_code} updated in Shopware (categories: {len(product_payload.get('categories', []))})")
            except BaseException as patch_error:
                # Note: ShopwareAPIError inherits from BaseException, not Exception!
                error_str = str(patch_error)
                # Handle Shopware VariantListingUpdater bug (Cardinality violation 1242)
                if "1242" in error_str and "Cardinality" in error_str:
                    frappe.logger().warning(
                        f"Cardinality violation for {template_item.item_code} - deleting and recreating product"
                    )
                    # Delete corrupted product and recreate
                    _delete_and_recreate_product(client, product_id, product_payload, template_item.item_code)
                else:
                    raise
        else:
            try:
                client.request_post("product", product_payload)
            except Exception as e:
                error_str = str(e).lower()
                # If product already exists in Shopware but not in ERPNext mapping, update instead
                if "updatecommand" in error_str or "already exists" in error_str:
                    get_logger().warning(f"Product {product_id} already exists in Shopware, updating instead")
                    # Remove ID from payload for PATCH
                    update_payload = {k: v for k, v in product_payload.items() if k != "id"}
                    client.request_patch(f"product/{product_id}", update_payload)
                else:
                    raise

        # Create Ecommerce Item link
        if not get_shopware_document_id("Item", template_item.name):
            frappe.get_doc({
                "doctype": "Ecommerce Item",
                "integration": MODULE_NAME,
                "erpnext_item_code": template_item.name,
                "integration_item_code": product_id,
                "has_variants": 1,
                "sku": template_item.item_code,
            }).insert(ignore_permissions=True)

        create_shopware_log(
            status="Success",
            request_data=product_payload,
            message=f"Created template product: {template_item.name} -> {product_id}",
            method="upload_template_item_to_shopware"
        )

        # Sync images
        if getattr(setting, 'sync_images_to_shopware', True):
            sync_product_images_to_shopware(client, template_item, product_id)

        return product_id

    except Exception as e:
        create_shopware_log(
            status="Error",
            message=f"Failed to upload template {template_item.name}: {str(e)}",
            exception=str(e),
            method="upload_template_item_to_shopware",
            rollback=True
        )
        get_logger().error(f"Shopware template upload failed for {template_item.name}: {e}", persist=False)
        return None


def cleanup_orphaned_variants(client, template_item_code: str, parent_shopware_id: str) -> dict:
    """
    Delete variants in Shopware that no longer exist in ERPNext.

    Args:
        client: Shopware API client
        template_item_code: ERPNext template item code
        parent_shopware_id: Shopware parent product ID

    Returns:
        Dict with cleanup statistics
    """
    stats = {"checked": 0, "deleted": 0, "errors": []}

    try:
        # Get ERPNext variant item codes (including disabled - they sync as active=false)
        erpnext_variants = set(frappe.get_all(
            "Item",
            filters={"variant_of": template_item_code},
            pluck="name"
        ))

        # Get Shopware child products
        response = client.request_post("search/product", {
            "filter": [{"type": "equals", "field": "parentId", "value": parent_shopware_id}],
            "limit": 500
        })
        shopware_variants = response.get("data", [])
        stats["checked"] = len(shopware_variants)

        for sw_variant in shopware_variants:
            product_number = sw_variant.get("productNumber", "")
            sw_variant_id = sw_variant.get("id")

            # Check if this variant exists in ERPNext
            if product_number not in erpnext_variants:
                try:
                    # Delete from Shopware
                    client.request_delete(f"product/{sw_variant_id}")
                    stats["deleted"] += 1
                    frappe.logger().info(
                        f"Deleted orphaned variant {product_number} from Shopware"
                    )

                    # Delete Ecommerce Item link if exists
                    ecom_item = frappe.db.exists("Ecommerce Item", {
                        "integration": MODULE_NAME,
                        "integration_item_code": sw_variant_id
                    })
                    if ecom_item:
                        frappe.delete_doc("Ecommerce Item", ecom_item, ignore_permissions=True)

                except Exception as e:
                    stats["errors"].append({
                        "variant": product_number,
                        "error": str(e)[:100]
                    })

        if stats["deleted"] > 0:
            create_shopware_log(
                status="Success",
                message=f"Cleanup: Deleted {stats['deleted']} orphaned variants for {template_item_code}",
                method="cleanup_orphaned_variants"
            )

    except Exception as e:
        stats["errors"].append({"error": str(e)[:200]})
        get_logger().error(f"Variant cleanup failed for {template_item_code}: {e}", persist=False)

    return stats


# =============================================================================
# BATCH ORPHAN VARIANT CLEANUP (using Shopware Sync API for faster deletion)
# =============================================================================

VARIANT_BATCH_SIZE = 100  # Variants per sync request
VARIANT_BATCH_DELAY = 0.05  # 50ms delay between batches


def _log_failed_variant_items(failed_ids: List[str], error_msg: str) -> None:
    """
    Persist failed variant deletions to database for later retry.

    Args:
        failed_ids: List of Shopware variant IDs that failed
        error_msg: Error message describing the failure
    """
    if not failed_ids:
        return

    try:
        create_shopware_log(
            status="Error",
            method="variant_batch.delete",
            message=f"Failed to delete {len(failed_ids)} orphaned variants: {error_msg}",
            request_data={
                "operation": "orphan_variant_delete",
                "failed_count": len(failed_ids),
                "failed_ids": failed_ids[:100],
                "retry_available": True
            }
        )
    except Exception:
        pass


def cleanup_all_orphaned_variants_batch(client, progress_callback=None) -> Dict[str, Any]:
    """
    Batch-delete all orphaned variants across all templates using Shopware Sync API.

    This is much faster than individual DELETE calls when there are many orphans.

    Args:
        client: Shopware API client
        progress_callback: Optional callback(template_num, total_templates, deleted_count)

    Returns:
        Dict with cleanup statistics
    """
    import time

    logger = get_logger("cleanup_orphaned_variants_batch")

    stats = {
        "templates_checked": 0,
        "variants_checked": 0,
        "variants_deleted": 0,
        "ecom_items_deleted": 0,
        "failed_items": [],
        "errors": []
    }

    # Get all template items
    template_items = frappe.get_all(
        "Ecommerce Item",
        filters={"integration": MODULE_NAME, "has_variants": 1},
        fields=["erpnext_item_code", "integration_item_code"]
    )

    stats["templates_checked"] = len(template_items)
    logger.info(f"Checking orphaned variants for {len(template_items)} templates")

    # Collect all variants to delete
    variants_to_delete = []  # List of {"id": shopware_id}
    ecom_items_to_delete = []  # List of Ecommerce Item names

    for idx, template in enumerate(template_items):
        template_code = template.erpnext_item_code
        parent_shopware_id = template.integration_item_code

        try:
            # Get ERPNext variant item codes (including disabled - they sync as active=false)
            erpnext_variants = set(frappe.get_all(
                "Item",
                filters={"variant_of": template_code},
                pluck="name"
            ))

            # Get Shopware child products
            response = client.request_post("search/product", {
                "filter": [{"type": "equals", "field": "parentId", "value": parent_shopware_id}],
                "limit": 500
            })
            shopware_variants = response.get("data", [])
            stats["variants_checked"] += len(shopware_variants)

            # Find orphans (in Shopware but not in ERPNext)
            for sw_variant in shopware_variants:
                product_number = sw_variant.get("productNumber", "")
                sw_variant_id = sw_variant.get("id")

                if product_number not in erpnext_variants:
                    variants_to_delete.append({"id": sw_variant_id})

                    # Check for Ecommerce Item link
                    ecom_item = frappe.db.exists("Ecommerce Item", {
                        "integration": MODULE_NAME,
                        "integration_item_code": sw_variant_id
                    })
                    if ecom_item:
                        ecom_items_to_delete.append(ecom_item)

        except Exception as e:
            stats["errors"].append({
                "template": template_code,
                "error": str(e)[:150]
            })

        if progress_callback and (idx + 1) % 10 == 0:
            progress_callback(idx + 1, len(template_items), len(variants_to_delete))

    if not variants_to_delete:
        logger.info("No orphaned variants found")
        return {"success": True, "statistics": stats}

    logger.info(f"Found {len(variants_to_delete)} orphaned variants to delete")

    # Batch delete via Sync API with rate limiting
    total_batches = (len(variants_to_delete) + VARIANT_BATCH_SIZE - 1) // VARIANT_BATCH_SIZE

    for i in range(0, len(variants_to_delete), VARIANT_BATCH_SIZE):
        batch = variants_to_delete[i:i + VARIANT_BATCH_SIZE]
        batch_num = i // VARIANT_BATCH_SIZE + 1

        try:
            sync_payload = {
                "delete-orphan-variants": {
                    "entity": "product",
                    "action": "delete",
                    "payload": batch
                }
            }

            response = client.request_post(
                "_action/sync",
                sync_payload,
                update_header_fields=HEADER_index_asynchronously
            )

            # Validate Sync API response
            not_found = response.get("notFound", []) if response else []
            successful = len(batch) - len(not_found)
            stats["variants_deleted"] += successful

            if not_found:
                logger.warning(f"Sync API: {len(not_found)} variants not found for deletion")
                stats["failed_items"].extend(not_found)

            logger.info(f"Deleted batch {batch_num}/{total_batches}: {successful}/{len(batch)} orphaned variants")

        except Exception as e:
            failed_ids = [item["id"] for item in batch]
            stats["failed_items"].extend(failed_ids)
            stats["errors"].append({
                "batch": batch_num,
                "error": str(e)[:200]
            })
            logger.error(f"Failed to delete variant batch: {e}")

            # Persist to database for later retry
            _log_failed_variant_items(failed_ids, str(e)[:150])

        # Rate limiting
        if batch_num < total_batches:
            time.sleep(VARIANT_BATCH_DELAY)

    # Delete Ecommerce Item links
    for ecom_name in ecom_items_to_delete:
        try:
            frappe.delete_doc("Ecommerce Item", ecom_name, ignore_permissions=True)
            stats["ecom_items_deleted"] += 1
        except Exception:
            pass

    frappe.db.commit()

    logger.success(
        f"Orphan variant cleanup complete: {stats['variants_deleted']} variants deleted, "
        f"{stats['ecom_items_deleted']} Ecommerce Items removed"
    )

    if stats["variants_deleted"] > 0:
        create_shopware_log(
            status="Success",
            message=f"Batch cleanup: Deleted {stats['variants_deleted']} orphaned variants",
            method="cleanup_all_orphaned_variants_batch"
        )

    # Standardized return: success=False if there were errors
    return {"success": len(stats["errors"]) == 0, "statistics": stats}
