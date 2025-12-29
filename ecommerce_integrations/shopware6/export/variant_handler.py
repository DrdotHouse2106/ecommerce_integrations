"""
Shopware 6 Variant Handler

Handles variant product upload for products that are variants of a template.
Creates variants as child products linked to the parent product.
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
    get_cached_currency_id,
)
from ecommerce_integrations.shopware6.export.property_handler import (
    get_or_create_property_group,
    get_or_create_variant_option,
    get_variant_attribute_values,
)
from ecommerce_integrations.shopware6.export.image_handler import sync_product_images_to_shopware


def upload_variant_item_to_shopware(client, variant_item) -> Optional[str]:
    """
    Export an ERPNext variant item to Shopware as a child product.

    Links the variant to its parent product using the options array.

    Args:
        client: Shopware API client
        variant_item: ERPNext Item document (variant)

    Returns:
        Shopware product ID if successful, None otherwise
    """
    from ecommerce_integrations.shopware6.validators import ShopwareDataValidator

    # Validate variant before upload (skip if no price, except RABATT items)
    is_valid, errors = ShopwareDataValidator.validate_item_for_export(variant_item.name)
    if not is_valid:
        for error in errors:
            frappe.logger("shopware6").info(f"Skipping variant {variant_item.name}: {error}")
        return None

    setting = frappe.get_cached_doc(SETTING_DOCTYPE)

    # Check if already synced
    shopware_product_id = get_shopware_document_id("Item", variant_item.name)
    if shopware_product_id:
        return shopware_product_id

    # Get parent product ID
    parent_id = get_shopware_document_id("Item", variant_item.variant_of)
    if not parent_id:
        # Parent not synced yet, sync it first
        from ecommerce_integrations.shopware6.export.template_handler import (
            upload_template_item_to_shopware
        )
        parent_item = frappe.get_doc("Item", variant_item.variant_of)
        parent_id = upload_template_item_to_shopware(client, parent_item)

        if not parent_id:
            frappe.log_error(
                f"Cannot upload variant {variant_item.name}: parent {variant_item.variant_of} not synced"
            )
            return None

    try:
        # Build payload
        product_payload = map_erpnext_item_to_shopware(variant_item)
        product_id = generate_uuid(f"product_{variant_item.item_code}")
        product_payload["id"] = product_id
        product_payload["parentId"] = parent_id

        # Currency
        currency_id = get_cached_currency_id(client, "EUR")
        if currency_id and product_payload.get("price"):
            product_payload["price"][0]["currencyId"] = currency_id

        # Tax
        tax_rate = product_payload.pop("_tax_rate", 19.0)
        tax_id = get_tax_id_by_rate(client, tax_rate)
        if tax_id:
            product_payload["taxId"] = tax_id

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

        # Check if product exists
        product_exists = False
        try:
            client.request_get(f"product/{product_id}")
            product_exists = True
        except BaseException:
            pass

        if product_exists:
            client.request_patch(f"product/{product_id}", product_payload)
            frappe.logger().info(f"Variant {variant_item.item_code} updated in Shopware")
        else:
            client.request_post("product", product_payload)

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
            message=f"Failed to upload variant {variant_item.name}: {str(e)}",
            exception=str(e),
            method="upload_variant_item_to_shopware",
            rollback=True
        )
        frappe.log_error(f"Shopware variant upload failed for {variant_item.name}: {e}")
        return None


def sync_all_variants(template_item_code: str) -> int:
    """
    Sync all variants of a template item to Shopware.

    Args:
        template_item_code: Template item code

    Returns:
        Number of variants synced
    """
    variants = frappe.get_all(
        "Item",
        filters={"variant_of": template_item_code, "disabled": 0},
        pluck="name"
    )

    synced = 0
    for variant_code in variants:
        variant_item = frappe.get_doc("Item", variant_code)
        product_id = upload_variant_item_to_shopware(variant_item)
        if product_id:
            synced += 1

    return synced
