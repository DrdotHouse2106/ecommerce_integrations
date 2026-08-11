"""
Shopware 6 Customer package.

Re-exports the public surface so existing imports keep working after the
``customer.py`` god-module was split into core / mapping / sync / vat.
External callers and the webhook event-mapper reference module attributes
like ``shopware6.customer.sync_customer_from_webhook`` — package attribute
lookup resolves those through this ``__init__``.
"""

from .core import (
    CustomerSyncInProgressError,
    ShopwareCustomer,
    _acquire_customer_sync_lock,
)
from .mapping import (
    _map_address_fields,
    create_customer_address,
    create_customer_contact,
    map_shopware_salutation,
)
from .sync import (
    _ensure_customer_contact,
    create_customer_from_shopware_data,
    create_guest_customer,
    ensure_customer_has_address,
    get_customer_from_shopware_order,
    sync_customer_by_id,
    sync_customer_from_webhook,
    sync_customers_from_shopware,
    sync_old_customers,
)
from .vat import (
    _find_existing_customer_to_link,
    _sync_customer_number_to_shopware,
    trigger_vat_id_check,
)
from .numbering import (
    allocate_next_customer_number,
    autoname_customer_for_shopware_push,
    seed_customer_number_counter,
)
from .push import push_customer_to_shopware, queue_customer_for_sync

__all__ = [
    "CustomerSyncInProgressError",
    "ShopwareCustomer",
    "_acquire_customer_sync_lock",
    "_ensure_customer_contact",
    "_find_existing_customer_to_link",
    "_map_address_fields",
    "_sync_customer_number_to_shopware",
    "allocate_next_customer_number",
    "autoname_customer_for_shopware_push",
    "create_customer_address",
    "create_customer_contact",
    "create_customer_from_shopware_data",
    "create_guest_customer",
    "ensure_customer_has_address",
    "get_customer_from_shopware_order",
    "map_shopware_salutation",
    "push_customer_to_shopware",
    "queue_customer_for_sync",
    "seed_customer_number_counter",
    "sync_customer_by_id",
    "sync_customer_from_webhook",
    "sync_customers_from_shopware",
    "sync_old_customers",
    "trigger_vat_id_check",
]
