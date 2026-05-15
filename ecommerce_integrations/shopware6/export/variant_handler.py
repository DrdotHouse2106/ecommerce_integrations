"""
Shopware 6 Variant Handler

Handles variant product upload for products that are variants of a template.
Creates variants as child products linked to the parent product.
"""


import frappe

from ecommerce_integrations.shopware6.constants import MODULE_NAME, SETTING_DOCTYPE
from ecommerce_integrations.shopware6.export.category_handler import (
    clear_product_categories,
    sync_all_item_categories,
)
from ecommerce_integrations.shopware6.export.image_handler import sync_product_images_to_shopware
from ecommerce_integrations.shopware6.export.product_mapper import (
    build_channel_prices,
    get_cached_currency_id,
    get_cached_sales_channel_id,
    get_or_create_delivery_time,
    get_product_visibilities,
    get_tax_id_by_rate,
    map_erpnext_item_to_shopware,
)
from ecommerce_integrations.shopware6.export.property_handler import (
    clear_product_options,
    clear_product_properties,
    get_item_custom_fields,
    get_item_properties,
    get_or_create_property_group,
    get_or_create_property_option,
    get_or_create_variant_option,
    get_variant_attribute_values,
)
from ecommerce_integrations.shopware6.export.utils import generate_uuid, get_shopware_document_id
from ecommerce_integrations.shopware6.utils import create_shopware_log, get_logger


def upload_variant_item_to_shopware(client, variant_item) -> str | None:
    """
    Export an ERPNext variant item to Shopware as a child product.

    Links the variant to its parent product using the options array.

    Args:
        client: Shopware API client
        variant_item: ERPNext Item document (variant)

    Returns:
        Shopware product ID if successful, None otherwise
    """
    from ecommerce_integrations.shopware6.validators import ERPNextDataValidator

    # Validate variant before upload (skip if no price, except RABATT items)
    is_valid, errors = ERPNextDataValidator.validate_item_for_export(variant_item.name)
    if not is_valid:
        for error in errors:
            frappe.logger("shopware6").info(f"Skipping variant {variant_item.name}: {error}")
        return None

    setting = frappe.get_cached_doc(SETTING_DOCTYPE)

    # Check if already synced - but continue to update if needed
    existing_shopware_id = get_shopware_document_id("Item", variant_item.name)

    # Get parent product ID
    parent_id = get_shopware_document_id("Item", variant_item.variant_of)
    if not parent_id:
        # Parent not synced yet, sync it first
        from ecommerce_integrations.shopware6.export.template_handler import upload_template_item_to_shopware
        parent_item = frappe.get_doc("Item", variant_item.variant_of)
        parent_id = upload_template_item_to_shopware(client, parent_item)

        if not parent_id:
            get_logger().error("Error occurred", persist=False)
            return None

    try:
        # Build payload
        product_payload = map_erpnext_item_to_shopware(variant_item)
        product_id = existing_shopware_id or generate_uuid(f"product_{variant_item.item_code}")
        product_payload["id"] = product_id
        product_payload["parentId"] = parent_id

        # Currency
        currency_id = get_cached_currency_id(client, "EUR")

        # Tax
        tax_rate = product_payload.pop("_tax_rate", 19.0)
        tax_id = get_tax_id_by_rate(client, tax_rate)
        if tax_id:
            product_payload["taxId"] = tax_id

        # Get parent item to determine visibility/channels for pricing
        parent_item = frappe.get_doc("Item", variant_item.variant_of)
        visibilities = get_product_visibilities(parent_item, setting)

        # Build channel-specific prices with Shopware Rules
        if visibilities and len(setting.sales_channels or []) > 0:
            # Multi-channel mode: use build_channel_prices for variant
            channel_prices = build_channel_prices(
                item=variant_item,  # Use variant for price lookup
                visibilities=visibilities,  # Use parent's visibility
                setting=setting,
                client=client,
                tax_rate=tax_rate,
                currency_id=currency_id
            )
            product_payload["prices"] = channel_prices
            # Remove single price, use prices array instead
            product_payload.pop("price", None)
        else:
            # Single-channel mode: keep original price logic
            if currency_id and product_payload.get("price"):
                product_payload["price"][0]["currencyId"] = currency_id

        # Sales channel visibility - Multi-Storefront support
        # Variants need their own visibilities (they don't inherit from parent in Shopware)
        if visibilities:
            product_payload["visibilities"] = visibilities
        else:
            # Fallback to legacy single-channel mode
            sales_channel_id = get_cached_sales_channel_id(client)
            if sales_channel_id:
                product_payload["visibilities"] = [{
                    "salesChannelId": sales_channel_id,
                    "visibility": 30
                }]

        # Build options array from variant attributes
        attr_values = get_variant_attribute_values(variant_item)
        options = []

        for attr_name, attr_value in attr_values.items():
            group_id = get_or_create_property_group(client, attr_name)
            if group_id:
                option_id = get_or_create_variant_option(client, group_id, attr_name, attr_value)
                if option_id:
                    options.append({"id": option_id})

        if options:
            product_payload["options"] = options

        # Properties (filter properties like Material, Farbe etc.)
        properties = get_item_properties(variant_item)
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

        # Custom Fields (Zubehör, AI fields, etc.)
        custom_fields = get_item_custom_fields(variant_item)
        if custom_fields:
            product_payload["customFields"] = custom_fields

        # Delivery Time
        delivery_time_str = getattr(variant_item, 'delivery_time', None)
        if delivery_time_str:
            delivery_time_id = get_or_create_delivery_time(client, delivery_time_str)
            if delivery_time_id:
                product_payload["deliveryTimeId"] = delivery_time_id

        # Categories - Variants need their own category assignment in Shopware
        # (they don't inherit from parent automatically)
        # Use skip_sync=True for fast lookup (categories synced in bulk Phase 1)
        category_ids = sync_all_item_categories(client, variant_item.item_code, skip_sync=True)
        if category_ids:
            product_payload["categories"] = category_ids

        # Always check if product exists in Shopware (API call to verify)
        product_exists = False
        try:
            client.request_get(f"product/{product_id}")
            product_exists = True
        except BaseException:
            pass

        if product_exists:
            # CRITICAL: Clear ALL old advanced/rule-based prices before setting new ones
            # This ensures variant has ONLY prices from ERPNext
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
            # Clear old OPTIONS before setting new ones (prevents duplicates from orphaned groups)
            if product_payload.get("options"):
                try:
                    clear_product_options(client, product_id)
                except BaseException:
                    pass  # Option clearing is optional, don't break sync
            # Clear old properties before setting new ones (don't let errors break sync)
            if product_payload.get("properties"):
                try:
                    clear_product_properties(client, product_id)
                except BaseException:
                    pass  # Property clearing is optional, don't break sync
            # Clear old categories before setting new ones
            if product_payload.get("categories"):
                try:
                    clear_product_categories(client, product_id)
                except BaseException:
                    pass  # Category clearing is optional, don't break sync
            # Clear old visibilities before setting new ones (prevents duplicate key errors)
            if product_payload.get("visibilities"):
                try:
                    vis_response = client.request_post("search/product-visibility", {
                        "filter": [{"type": "equals", "field": "productId", "value": product_id}],
                        "limit": 100
                    })
                    for vis_entry in vis_response.get("data", []):
                        try:
                            client.request_delete(f"product-visibility/{vis_entry.get('id')}")
                        except Exception:
                            pass
                except BaseException:
                    pass  # Visibility clearing is optional, don't break sync
            client.request_patch(f"product/{product_id}", product_payload)
            frappe.logger().info(f"Variant {variant_item.item_code} updated in Shopware")
        else:
            try:
                client.request_post("product", product_payload)
                frappe.logger().info(f"Variant {variant_item.item_code} created in Shopware")
            except Exception as e:
                error_str = str(e).lower()
                # If product already exists in Shopware but not in ERPNext mapping, update instead
                if "updatecommand" in error_str or "already exists" in error_str:
                    get_logger().warning(f"Variant {product_id} already exists in Shopware, updating instead")
                    # Remove ID from payload for PATCH
                    update_payload = {k: v for k, v in product_payload.items() if k != "id"}
                    client.request_patch(f"product/{product_id}", update_payload)
                    frappe.logger().info(f"Variant {variant_item.item_code} updated in Shopware")
                else:
                    raise

        # Create Ecommerce Item link
        if not get_shopware_document_id("Item", variant_item.name):
            frappe.get_doc({
                "doctype": "Ecommerce Item",
                "integration": MODULE_NAME,
                "erpnext_item_code": variant_item.name,
                "integration_item_code": product_id,
                "variant_id": product_id,
                "variant_of": variant_item.variant_of,
                "sku": variant_item.item_code,
            }).insert(ignore_permissions=True)

        create_shopware_log(
            status="Success",
            request_data=product_payload,
            message=f"Created variant product: {variant_item.name} -> {product_id}",
            method="upload_variant_item_to_shopware"
        )

        # Sync images
        if getattr(setting, 'sync_images_to_shopware', True):
            sync_product_images_to_shopware(client, variant_item, product_id)

        return product_id

    except Exception as e:
        create_shopware_log(
            status="Error",
            message=f"Failed to upload variant {variant_item.name}: {e!s}",
            exception=str(e),
            method="upload_variant_item_to_shopware",
            rollback=True
        )
        get_logger().error(f"Shopware variant upload failed for {variant_item.name}: {e}", persist=False)
        return None


def sync_all_variants(client, template_item_code: str) -> int:
    """
    Sync all variants of a template item to Shopware.

    Args:
        client: Shopware API client
        template_item_code: Template item code

    Returns:
        Number of variants synced
    """
    # Include disabled variants - they will be synced with active=false
    variants = frappe.get_all(
        "Item",
        filters={"variant_of": template_item_code},
        pluck="name"
    )

    synced = 0
    for variant_code in variants:
        variant_item = frappe.get_doc("Item", variant_code)
        product_id = upload_variant_item_to_shopware(client, variant_item)
        if product_id:
            synced += 1

    return synced
