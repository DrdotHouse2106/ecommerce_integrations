"""Hook entry points for Shopware product sync queueing.

All four ``queue_*_for_sync`` handlers fan an Item / Item Price /
Item Group save out to the **new product_sync engine** via
``product_sync.tasks.dispatch_item_change``. The legacy single-item
uploader (``bulk_sync.sync_single_item_to_shopware``,
``product_export.update_item_price_in_shopware``,
``properties.upload_item_properties``) is no longer reachable from
the doc-event path — that path is the single source of truth for
"item saved → sync to backend" and lives entirely inside
``product_sync/``. Operators get the same delta-hash gate, the same
canonical payload and the same audit row whether the change came
from the cron, the dispatch button or a per-Item save.
"""

import frappe

from ecommerce_integrations.shopware6.constants import ROOT_ITEM_GROUPS


def queue_item_for_sync(doc, method=None):
    """Enqueue a single-item product-sync dispatch on Item save.

    Pre-flight gates (in order):

    * skip integration-originated writes (``flags.from_integration``)
      so a Shopware import doesn't bounce back to Shopware,
    * skip when the global ``skip_shopware_sync`` flag is set,
    * skip when Shopware is disabled in Setting or the per-item
      enable-on-create / enable-on-update gate is off,
    * skip when the Item is in no active Product Sync scope — the
      Product Sync doctype is the explicit opt-in for "which Items
      get pushed", so a save on an out-of-scope item is a noop here.
    """
    item = doc

    if item.flags.from_integration:
        return

    if getattr(frappe.flags, "skip_shopware_sync", False):
        return

    if not _is_shopware_item_sync_enabled(item.name):
        return

    if not _item_in_any_active_sync(item):
        return

    _enqueue_product_sync_dispatch(item.name)


def queue_item_delete_for_sync(doc, method=None):
    """Queue item deactivation in Shopware.

    Deletion / deactivation isn't a delta drift — it's an explicit
    backend command — so it stays on its own enqueued path rather
    than going through the dispatch. The new product_sync adapter
    exposes a ``deactivate_product`` for this; until that path is
    plumbed through, the legacy ``deactivate_product_in_shopware``
    is still the entry point.
    """
    item = doc

    if getattr(frappe.flags, "skip_shopware_sync", False):
        return

    if not _is_shopware_enabled():
        return

    frappe.enqueue(
        "ecommerce_integrations.shopware6.product_export.deactivate_product_in_shopware",
        queue="short",
        item_code=item.name,
        enqueue_after_commit=True,
    )


def queue_properties_for_sync(doc, method=None):
    """Property change is part of the same item canonical → same dispatch.

    The new engine's canonical hashes properties into the
    ``properties`` section. A property change therefore flips the
    Item's overall canonical hash and the dispatch picks it up via
    the same drift gate as any other field change. No separate
    per-property push path is needed.
    """
    item = doc

    if item.flags.from_integration:
        return

    if getattr(frappe.flags, "skip_shopware_sync", False):
        return

    if not _is_shopware_enabled(check_update_setting=True):
        return

    if not _item_in_any_active_sync(item):
        return

    _enqueue_product_sync_dispatch(item.name)


def queue_item_group_for_sync(doc, method=None):
    """Queue an Item Group for category sync.

    Category sync still flows through the legacy category handler —
    the new engine doesn't own category trees yet (that's Catalog
    Mirror's job). Left as-is until Catalog Mirror absorbs the
    per-channel write side.
    """
    item_group = doc

    if item_group.name in ROOT_ITEM_GROUPS:
        return

    if getattr(frappe.flags, "skip_shopware_sync", False):
        return

    if not _is_shopware_enabled():
        return

    frappe.enqueue(
        "ecommerce_integrations.shopware6.product_export.sync_item_group_to_shopware",
        queue="short",
        item_group_name=item_group.name,
        enqueue_after_commit=True,
    )


def queue_item_group_rename_for_sync(doc, method=None, old_name=None, new_name=None):
    """Queue an Item Group rename for Shopware sync."""
    previous_name, current_name = _resolve_item_group_rename_names(doc, method, old_name, new_name)
    if not previous_name or not current_name:
        return

    if previous_name in ROOT_ITEM_GROUPS or current_name in ROOT_ITEM_GROUPS:
        return

    if getattr(frappe.flags, "skip_shopware_sync", False):
        return

    if not _is_shopware_enabled():
        return

    frappe.enqueue(
        "ecommerce_integrations.shopware6.product_export.rename_category_in_shopware",
        queue="short",
        old_name=previous_name,
        new_name=current_name,
        enqueue_after_commit=True,
    )


def queue_item_group_delete_for_sync(doc, method=None):
    """Queue Item Group deletion from Shopware."""
    item_group = doc

    if item_group.name in ROOT_ITEM_GROUPS:
        return

    if getattr(frappe.flags, "skip_shopware_sync", False):
        return

    if not _is_shopware_enabled():
        return

    frappe.enqueue(
        "ecommerce_integrations.shopware6.product_export.delete_category_from_shopware",
        queue="short",
        category_name=item_group.name,
        enqueue_after_commit=True,
    )


def queue_price_for_sync(doc, method=None):
    """Enqueue dispatch when an Item Price changes.

    No per-price-list filtering here — the new engine's canonical
    decides whether the price-list change is relevant (matches the
    Sync's ``price_list_override`` or a per-channel override). An
    irrelevant price-list change becomes a cheap noop via the hash
    gate; relevant ones flip the hash and trigger a push.
    """
    item_price = doc

    if item_price.flags.from_integration:
        return

    if getattr(frappe.flags, "skip_shopware_sync", False):
        return

    if not _is_shopware_enabled():
        return

    item_code = item_price.item_code
    if not item_code:
        return

    if not _item_code_in_any_active_sync(item_code):
        return

    _enqueue_product_sync_dispatch(item_code)


def _get_shopware_setting():
    try:
        from ecommerce_integrations.shopware6.constants import SETTING_DOCTYPE

        setting = frappe.get_cached_doc(SETTING_DOCTYPE)
        if not setting.is_enabled():
            return None
        return setting
    except Exception:
        return None


def _is_shopware_enabled(check_update_setting: bool = False) -> bool:
    setting = _get_shopware_setting()
    if not setting:
        return False
    if check_update_setting and not getattr(setting, "update_shopware_item_on_update", True):
        return False
    return True


def _is_shopware_item_sync_enabled(item_code: str) -> bool:
    setting = _get_shopware_setting()
    if not setting:
        return False

    from ecommerce_integrations.shopware6.utils import get_shopware_document_id

    is_new_product = not bool(get_shopware_document_id("Item", item_code))
    if is_new_product:
        return bool(getattr(setting, "upload_erpnext_items", False))
    return bool(getattr(setting, "update_shopware_item_on_update", True))


def _enqueue_product_sync_dispatch(item_code: str) -> None:
    """Enqueue the unified ``dispatch_item_change`` for one item.

    Job-id + ``deduplicate=True`` collapses rapid double-saves of
    the same item into one queued job — same dedup contract the
    legacy single-item enqueue had. ``enqueue_after_commit=True``
    so the job sees the persisted state, not the in-flight one.
    """
    frappe.enqueue(
        "ecommerce_integrations.product_sync.tasks.dispatch_item_change",
        queue="short",
        item_code=item_code,
        backend="Shopware",
        enqueue_after_commit=True,
        job_id=f"product_sync:dispatch:shopware:{item_code}",
        deduplicate=True,
    )


def _resolve_item_group_rename_names(doc, method=None, old_name=None, new_name=None):
    """Handle the different positional argument layouts used by Frappe rename hooks."""
    if not isinstance(doc, str):
        return None, None

    if isinstance(old_name, str) and isinstance(new_name, str):
        return old_name, new_name

    if isinstance(method, str) and isinstance(old_name, str):
        return method, old_name

    return None, None


def _item_in_any_active_sync(doc) -> bool:
    """Delegate to the shared two-stage scope gate in product_sync.

    Kept here as a thin wrapper so the hook signature in ``hooks.py``
    doesn't change. The cheap IG pre-check inside the shared gate is
    what keeps bulk Item-save bursts fast.
    """
    from ecommerce_integrations.product_sync.constants import BACKEND_SHOPWARE
    from ecommerce_integrations.product_sync.resolver import (
        item_is_in_any_active_sync,
    )
    try:
        return item_is_in_any_active_sync(doc.name, BACKEND_SHOPWARE)
    except Exception:
        # Fail-open: a resolver glitch shouldn't drop legitimate syncs
        # silently. The downstream apply pipeline is itself idempotent
        # and will noop when there's actually nothing to push.
        return True


def _item_code_in_any_active_sync(item_code: str) -> bool:
    """``_item_in_any_active_sync`` variant that takes a string code
    rather than a doc — used by the price-change hook where we never
    instantiate the Item doc."""
    from ecommerce_integrations.product_sync.constants import BACKEND_SHOPWARE
    from ecommerce_integrations.product_sync.resolver import (
        item_is_in_any_active_sync,
    )
    try:
        return item_is_in_any_active_sync(item_code, BACKEND_SHOPWARE)
    except Exception:
        return True
