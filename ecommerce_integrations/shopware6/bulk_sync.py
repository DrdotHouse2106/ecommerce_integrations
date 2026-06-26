"""Shopware 6 doc-event hook entry points.

Historically this module also carried a Redis-backed bulk queue + batch
processor. That path is superseded by the delta product-sync engine
(``product_sync/``): an Item / Item Group save fans out through the
``queue_*_for_sync`` handlers in ``services.queue_hooks``, which dispatch a
single-item run through the same canonical → hash → diff → push pipeline as
the cron. The legacy queue and its batch uploader have been removed.

Only the hook-name forwarding remains: ``hooks.py`` declares the doc events as
``shopware6.bulk_sync.queue_*_for_sync``, so ``__getattr__`` resolves those
names to the real implementations in ``services.queue_hooks``.
"""

# Hook entry points are implemented in services.queue_hooks. __getattr__
# forwards them on attribute access so Frappe's hook dispatcher sees them at
# ``shopware6.bulk_sync.<name>`` as the doctype events declare in hooks.py.
_FORWARDED_TO_QUEUE_HOOKS = frozenset({
    "queue_item_for_sync",
    "queue_item_delete_for_sync",
    "queue_properties_for_sync",
    "queue_item_group_for_sync",
    "queue_item_group_rename_for_sync",
    "queue_item_group_delete_for_sync",
    "queue_price_for_sync",
})


def __getattr__(name):
    if name in _FORWARDED_TO_QUEUE_HOOKS:
        from ecommerce_integrations.shopware6.services import queue_hooks

        return getattr(queue_hooks, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
