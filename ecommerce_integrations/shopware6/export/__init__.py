"""
Shopware 6 Product Export Module.

Focused helpers used by the delta product-sync engine (``product_sync/``)
and the Item-Group category sync:
- utils: Utility functions (generate_uuid, sanitize_filename)
- property_handler: Property group and option management
- category_handler: Category hierarchy and sync
- product_mapper: Shared lookup/get-or-create helpers (tax, delivery
  time, manufacturer, unit, currency, sales channel)
- price_handler: Pricing helpers backing the whitelisted "force price
  resync" admin tools (Ecommerce Item-driven, not doc-event-triggered)

The legacy single-item uploader (``product_uploader``), the batch uploader,
the full-reconciliation module, the template/variant configurable-
product uploader (``template_handler``/``variant_handler``), and the
per-save image/price push (``image_handler``, most of ``price_handler``)
have all been removed — that path is the delta engine now.
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

# Price handling
from ecommerce_integrations.shopware6.export.price_handler import (
    get_item_price,
)

# Product mapper
from ecommerce_integrations.shopware6.export.product_mapper import (
    get_cached_currency_id,
    get_cached_sales_channel_id,
    get_or_create_manufacturer,
    get_tax_id_by_rate,
)

# Property handling
from ecommerce_integrations.shopware6.export.property_handler import (
    get_or_create_property_group,
    get_or_create_property_option,
)

# Utils
from ecommerce_integrations.shopware6.export.utils import (
    generate_uuid,
    get_field_mappings,
    get_shopware_document_id,
    sanitize_filename,
)

__all__ = [
    "delete_category_from_shopware",
    # Utils
    "generate_uuid",
    "get_cached_currency_id",
    "get_cached_sales_channel_id",
    "get_field_mappings",
    "get_item_price",
    "get_or_create_category",
    "get_or_create_manufacturer",
    # Properties
    "get_or_create_property_group",
    "get_or_create_property_option",
    "get_shopware_document_id",
    "get_tax_id_by_rate",
    "rename_category_in_shopware",
    "sanitize_filename",
    "sync_all_item_categories",
    # Categories
    "sync_category_hierarchy",
    "sync_item_group_to_shopware",
]
