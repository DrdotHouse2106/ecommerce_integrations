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
from ecommerce_integrations.shopware6.export.product_uploader import (
    ShopwareProductUploader,
    batch_sync_if_changed,
    deactivate_product_in_shopware,
    sync_item_if_changed,
    upload_erpnext_item_to_shopware,
)

# Property handling
from ecommerce_integrations.shopware6.export.property_handler import (
    ensure_shopware_custom_field_set,
    get_item_custom_fields,
    get_item_properties,
    get_or_create_property_group,
    get_or_create_property_option,
)

# Reconciliation
from ecommerce_integrations.shopware6.export.reconciliation import (
    cleanup_orphaned_shopware_categories,
    enqueue_full_reconciliation,
    enqueue_full_reconciliation_with_categories,
    full_reconciliation,
    reconcile_all_to_shopware,
    reconcile_erpnext_with_shopware,
    sync_all_categories_to_shopware,
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
    # Main class
    "ShopwareProductUploader",
    "batch_sync_if_changed",
    "cleanup_orphaned_shopware_categories",
    "deactivate_product_in_shopware",
    "delete_category_from_shopware",
    "enqueue_full_reconciliation",
    "enqueue_full_reconciliation_with_categories",
    "ensure_shopware_custom_field_set",
    "full_reconciliation",
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
    "reconcile_all_to_shopware",
    "reconcile_erpnext_with_shopware",
    "rename_category_in_shopware",
    "sanitize_filename",
    # Reconciliation
    "sync_all_categories_to_shopware",
    "sync_all_item_categories",
    "sync_all_variants",
    "sync_bulk_prices",
    # Categories
    "sync_category_hierarchy",
    "sync_item_group_to_shopware",
    "sync_item_if_changed",
    # Images
    "sync_product_images_to_shopware",
    # Prices
    "sync_product_price",
    "update_item_price_in_shopware",
    # Upload functions
    "upload_erpnext_item_to_shopware",
    "upload_media_to_shopware",
    "upload_template_item_to_shopware",
    "upload_variant_item_to_shopware",
]
