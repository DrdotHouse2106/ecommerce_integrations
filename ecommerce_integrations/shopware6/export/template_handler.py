"""
Shopware 6 Template Handler

Handles template/parent product upload for products with variants.
Creates the parent product with configuratorSettings for variant options.
"""

from typing import Optional

import frappe

from ecommerce_integrations.shopware6.connection import temp_shopware_session
from ecommerce_integrations.shopware6.constants import MODULE_NAME, SETTING_DOCTYPE
from ecommerce_integrations.shopware6.utils import create_shopware_log
from ecommerce_integrations.shopware6.export.utils import generate_uuid, get_shopware_document_id
from ecommerce_integrations.shopware6.export.product_mapper import (
    map_erpnext_item_to_shopware,
    get_tax_id_by_rate,
    get_or_create_manufacturer,
    get_cached_currency_id,
    get_cached_sales_channel_id,
    get_actual_variant_values,
)
from ecommerce_integrations.shopware6.export.category_handler import (
    sync_all_item_categories,
    sync_category_hierarchy,
)
from ecommerce_integrations.shopware6.export.property_handler import (
    get_or_create_property_group,
    get_or_create_variant_option,
    get_template_item_attributes,
)
from ecommerce_integrations.shopware6.export.image_handler import sync_product_images_to_shopware


def upload_template_item_to_shopware(client, template_item) -> Optional[str]:
    """
    Export an ERPNext template item (has_variants=1) to Shopware as a parent product.

    Creates the parent product with configuratorSettings for all variant options.

    Args:
        client: Shopware API client
        template_item: ERPNext Item document with has_variants=1

    Returns:
        Shopware product ID if successful, None otherwise
    """
    setting = frappe.get_cached_doc(SETTING_DOCTYPE)

    # Check if already synced
    shopware_product_id = get_shopware_document_id("Item", template_item.name)
    if shopware_product_id:
        return shopware_product_id

    try:
        # Build base payload
        product_payload = map_erpnext_item_to_shopware(template_item)
        product_id = generate_uuid(f"product_{template_item.item_code}")
        product_payload["id"] = product_id

        # Currency
        currency_id = get_cached_currency_id(client, "EUR")
        if currency_id and product_payload.get("price"):
            product_payload["price"][0]["currencyId"] = currency_id

        # Categories
        category_ids = sync_all_item_categories(client, template_item.item_code)
        if category_ids:
            product_payload["categories"] = category_ids
        elif template_item.item_group:
            category_id = sync_category_hierarchy(client, template_item.item_group)
            if category_id:
                product_payload["categories"] = [{"id": category_id}]

        # Sales channel visibility
        sales_channel_id = get_cached_sales_channel_id(client)
        if sales_channel_id:
            product_payload["visibilities"] = [{
                "salesChannelId": sales_channel_id,
                "visibility": 30
            }]

        # Tax
        tax_rate = product_payload.pop("_tax_rate", 19.0)
        tax_id = get_tax_id_by_rate(client, tax_rate)
        if tax_id:
            product_payload["taxId"] = tax_id

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

        # Check if product exists
        product_exists = False
        try:
            client.request_get(f"product/{product_id}")
            product_exists = True
        except BaseException:
            pass

        if product_exists:
            product_payload.pop("visibilities", None)
            client.request_patch(f"product/{product_id}", product_payload)
            frappe.logger().info(f"Template {template_item.item_code} updated in Shopware")
        else:
            client.request_post("product", product_payload)

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
        frappe.log_error(f"Shopware template upload failed for {template_item.name}: {e}")
        return None
