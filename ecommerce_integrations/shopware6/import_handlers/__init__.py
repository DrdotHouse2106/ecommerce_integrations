"""
Shopware 6 Import Handlers

Bi-directional sync support for importing data from Shopware to ERPNext.

Modules:
- property_importer: Import product properties and attributes
- stock_importer: Import stock levels (optional)

Both importers are disabled by default and must be enabled via Shopware Settings.
"""

from ecommerce_integrations.shopware6.import_handlers.property_importer import (
    PropertyImporter,
    import_properties_from_shopware,
    sync_property_groups_from_shopware,
    batch_import_properties,
)

from ecommerce_integrations.shopware6.import_handlers.stock_importer import (
    StockImporter,
    import_stock_from_shopware,
    sync_stock_for_product,
    handle_stock_webhook,
)

__all__ = [
    # Property import
    "PropertyImporter",
    "import_properties_from_shopware",
    "sync_property_groups_from_shopware",
    "batch_import_properties",
    # Stock import
    "StockImporter",
    "import_stock_from_shopware",
    "sync_stock_for_product",
    "handle_stock_webhook",
]
