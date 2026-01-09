"""
Shopware 6 Template Handler

Handles template/parent product upload for products with variants.
Creates the parent product with configuratorSettings for variant options.
"""

from typing import Optional

import frappe

from ecommerce_integrations.shopware6.constants import MODULE_NAME, SETTING_DOCTYPE
from ecommerce_integrations.shopware6.utils import create_shopware_log, get_logger
from ecommerce_integrations.shopware6.export.utils import generate_uuid, get_shopware_document_id
from ecommerce_integrations.shopware6.export.product_mapper import (
    map_erpnext_item_to_shopware,
    get_tax_id_by_rate,
    get_or_create_manufacturer,
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

        # Categories
        category_ids = sync_all_item_categories(client, template_item.item_code)
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
        if attributes:
            configurator_group_config = []
            for attr in attributes:
                attr_name = attr["name"]
                group_id = get_or_create_property_group(client, attr_name)
                if group_id:
                    configurator_group_config.append({
                        "id": group_id,
                        "expressionForListings": True,
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
        # Get ERPNext variant item codes
        erpnext_variants = set(frappe.get_all(
            "Item",
            filters={"variant_of": template_item_code, "disabled": 0},
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


def sync_template_with_variant_cleanup(client, template_item) -> dict:
    """
    Sync a template item and cleanup orphaned variants.

    This is the recommended function to use for full variant sync:
    1. Upload/update template product
    2. Sync all ERPNext variants
    3. Delete Shopware variants not in ERPNext

    Args:
        client: Shopware API client
        template_item: ERPNext Item document with has_variants=1

    Returns:
        Dict with sync and cleanup statistics
    """
    from ecommerce_integrations.shopware6.export.variant_handler import upload_variant_item_to_shopware

    result = {
        "template_id": None,
        "variants_synced": 0,
        "variants_deleted": 0,
        "errors": []
    }

    # Step 1: Upload/update template
    parent_id = upload_template_item_to_shopware(client, template_item)
    if not parent_id:
        result["errors"].append("Failed to sync template")
        return result

    result["template_id"] = parent_id

    # Step 2: Sync all ERPNext variants
    variants = frappe.get_all(
        "Item",
        filters={"variant_of": template_item.name, "disabled": 0},
        pluck="name"
    )

    for variant_code in variants:
        try:
            variant_item = frappe.get_doc("Item", variant_code)
            variant_id = upload_variant_item_to_shopware(client, variant_item)
            if variant_id:
                result["variants_synced"] += 1
        except Exception as e:
            result["errors"].append(f"{variant_code}: {str(e)[:100]}")

    # Step 3: Cleanup orphaned variants in Shopware
    cleanup_stats = cleanup_orphaned_variants(client, template_item.name, parent_id)
    result["variants_deleted"] = cleanup_stats.get("deleted", 0)
    result["errors"].extend(cleanup_stats.get("errors", []))

    frappe.logger().info(
        f"Template sync complete: {template_item.name} - "
        f"synced: {result['variants_synced']}, deleted: {result['variants_deleted']}"
    )

    return result
