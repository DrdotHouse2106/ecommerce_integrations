"""
Shopware 6 Product Export Module.

Focused helpers used by the delta product-sync engine (``product_sync/``)
and the Item-Group category sync:
- utils: Utility functions (generate_uuid, sanitize_filename)
- property_handler: Property group and option management
- category_handler: Category hierarchy and sync
- image_handler: Image upload and delta-sync
- product_mapper: ERPNext → Shopware field-mapping helpers
- template_handler / variant_handler: configurable-product helpers
- price_handler: Price synchronization

The legacy single-item uploader (``product_uploader``), the batch uploader and
the full-reconciliation module have been removed — that path is the delta
engine now.
"""

# Category handling
from ecommerce_integrations.shopware6.export.category_handler import (
    delete_category_from_shopware,
    get_or_create_category,
    rename_category_in_shopware,
    sync_all_item_categories,
    sync_category_hierarchy,
    sync_item_group_to_shopware,
)

# Image handling
from ecommerce_integrations.shopware6.export.image_handler import (
    sync_product_images_to_shopware,
    upload_media_to_shopware,
)

# Price handling
from ecommerce_integrations.shopware6.export.price_handler import (
    get_item_price,
    sync_bulk_prices,
    sync_product_price,
    update_item_price_in_shopware,
)

# Product mapper
from ecommerce_integrations.shopware6.export.product_mapper import (
    get_cached_currency_id,
    get_cached_sales_channel_id,
    get_or_create_manufacturer,
    get_tax_id_by_rate,
    map_erpnext_item_to_shopware,
)

# Property handling
from ecommerce_integrations.shopware6.export.property_handler import (
    ensure_shopware_custom_field_set,
    get_item_custom_fields,
    get_item_properties,
    get_or_create_property_group,
    get_or_create_property_option,
)

# Template/Variant handlers
from ecommerce_integrations.shopware6.export.template_handler import (
    upload_template_item_to_shopware,
)

# Utils
from ecommerce_integrations.shopware6.export.utils import (
    generate_uuid,
    get_field_mappings,
    get_shopware_document_id,
    sanitize_filename,
)
from ecommerce_integrations.shopware6.export.variant_handler import (
    sync_all_variants,
    upload_variant_item_to_shopware,
)

__all__ = [
    "delete_category_from_shopware",
    "ensure_shopware_custom_field_set",
    # Utils
    "generate_uuid",
    "get_cached_currency_id",
    "get_cached_sales_channel_id",
    "get_field_mappings",
    "get_item_custom_fields",
    "get_item_price",
    "get_item_properties",
    "get_or_create_category",
    "get_or_create_manufacturer",
    # Properties
    "get_or_create_property_group",
    "get_or_create_property_option",
    "get_shopware_document_id",
    "get_tax_id_by_rate",
    # Mapper
    "map_erpnext_item_to_shopware",
    "rename_category_in_shopware",
    "sanitize_filename",
    "sync_all_item_categories",
    "sync_all_variants",
    "sync_bulk_prices",
    # Categories
    "sync_category_hierarchy",
    "sync_item_group_to_shopware",
    # Images
    "sync_product_images_to_shopware",
    # Prices
    "sync_product_price",
    "update_item_price_in_shopware",
    # Upload helpers (configurable products)
    "upload_media_to_shopware",
    "upload_template_item_to_shopware",
    "upload_variant_item_to_shopware",
]
