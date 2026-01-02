"""
Shopware 6 Product Export Module

Refactored from the monolithic product_export.py (5446 lines) into focused modules:
- utils: Utility functions (generate_uuid, sanitize_filename)
- property_handler: Property group and option management
- category_handler: Category hierarchy and sync
- image_handler: Image upload and delta-sync
- product_mapper: ERPNext to Shopware field mapping
- product_uploader: Main ShopwareProductUploader class
- template_handler: Template/parent product handling
- variant_handler: Variant product handling
- price_handler: Price synchronization

Usage:
    from ecommerce_integrations.shopware6.export import ShopwareProductUploader

    uploader = ShopwareProductUploader(item_code="ITEM-001")
    uploader.upload()
"""

# Product uploader
from ecommerce_integrations.shopware6.export.product_uploader import (
    ShopwareProductUploader,
    upload_erpnext_item_to_shopware,
    sync_item_if_changed,
    batch_sync_if_changed,
)

# Template/Variant handlers
from ecommerce_integrations.shopware6.export.template_handler import (
    upload_template_item_to_shopware,
)
from ecommerce_integrations.shopware6.export.variant_handler import (
    upload_variant_item_to_shopware,
    sync_all_variants,
)

# Category handling
from ecommerce_integrations.shopware6.export.category_handler import (
    sync_category_hierarchy,
    sync_all_item_categories,
    get_or_create_category,
    sync_item_group_to_shopware,
)

# Image handling
from ecommerce_integrations.shopware6.export.image_handler import (
    sync_product_images_to_shopware,
    upload_media_to_shopware,
)

# Price handling
from ecommerce_integrations.shopware6.export.price_handler import (
    sync_product_price,
    sync_bulk_prices,
    update_item_price_in_shopware,
    get_item_price,
)

# Product mapper
from ecommerce_integrations.shopware6.export.product_mapper import (
    map_erpnext_item_to_shopware,
    get_tax_id_by_rate,
    get_or_create_manufacturer,
    get_cached_currency_id,
    get_cached_sales_channel_id,
)

# Property handling
from ecommerce_integrations.shopware6.export.property_handler import (
    get_or_create_property_group,
    get_or_create_property_option,
    get_item_properties,
    get_item_custom_fields,
    ensure_shopware_custom_field_set,
)

# Utils
from ecommerce_integrations.shopware6.export.utils import (
    generate_uuid,
    sanitize_filename,
    get_shopware_document_id,
    get_field_mappings,
)

# Reconciliation
from ecommerce_integrations.shopware6.export.reconciliation import (
    sync_all_categories_to_shopware,
    reconcile_erpnext_with_shopware,
    reconcile_all_to_shopware,
    full_reconciliation,
    enqueue_full_reconciliation,
    enqueue_full_reconciliation_with_categories,
    cleanup_orphaned_shopware_categories,
)

__all__ = [
    # Main class
    "ShopwareProductUploader",
    # Upload functions
    "upload_erpnext_item_to_shopware",
    "sync_item_if_changed",
    "batch_sync_if_changed",
    "upload_template_item_to_shopware",
    "upload_variant_item_to_shopware",
    "sync_all_variants",
    # Categories
    "sync_category_hierarchy",
    "sync_all_item_categories",
    "get_or_create_category",
    "sync_item_group_to_shopware",
    # Images
    "sync_product_images_to_shopware",
    "upload_media_to_shopware",
    # Prices
    "sync_product_price",
    "sync_bulk_prices",
    "update_item_price_in_shopware",
    "get_item_price",
    # Mapper
    "map_erpnext_item_to_shopware",
    "get_tax_id_by_rate",
    "get_or_create_manufacturer",
    "get_cached_currency_id",
    "get_cached_sales_channel_id",
    # Properties
    "get_or_create_property_group",
    "get_or_create_property_option",
    "get_item_properties",
    "get_item_custom_fields",
    "ensure_shopware_custom_field_set",
    # Utils
    "generate_uuid",
    "sanitize_filename",
    "get_shopware_document_id",
    "get_field_mappings",
    # Reconciliation
    "sync_all_categories_to_shopware",
    "reconcile_erpnext_with_shopware",
    "reconcile_all_to_shopware",
    "full_reconciliation",
    "enqueue_full_reconciliation",
    "enqueue_full_reconciliation_with_categories",
    "cleanup_orphaned_shopware_categories",
]
