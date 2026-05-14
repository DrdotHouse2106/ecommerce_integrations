"""Hook entry points for Shopware bulk-sync queueing."""

import frappe

from ecommerce_integrations.shopware6.constants import ROOT_ITEM_GROUPS


def queue_item_for_sync(doc, method=None):
    """Queue an item for product sync or enqueue a single async sync."""
    from ecommerce_integrations.shopware6.bulk_sync import (
        activate_bulk_mode,
        add_to_sync_queue,
        get_bulk_sync_settings,
        is_bulk_mode_active,
        schedule_bulk_sync_processing,
        should_use_bulk_mode,
    )

    item = doc

    if item.flags.from_integration:
        return

    if getattr(frappe.flags, "skip_shopware_sync", False):
        return

    if not _is_shopware_item_sync_enabled(item.name):
        return

    settings = get_bulk_sync_settings()

    if frappe.flags.in_import or getattr(frappe.flags, "in_bulk_update", False):
        add_to_sync_queue(item.name, "product")
        if not is_bulk_mode_active():
            activate_bulk_mode()
        return

    if not settings["enabled"]:
        _enqueue_shopware_product_sync(item.name)
        return

    if should_use_bulk_mode():
        add_to_sync_queue(item.name, "product")
        schedule_bulk_sync_processing()
        return

    _enqueue_shopware_product_sync(item.name)


def queue_item_delete_for_sync(doc, method=None):
    """Queue item deactivation for Shopware."""
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
    """Queue Shopware property sync for an item."""
    from ecommerce_integrations.shopware6.bulk_sync import (
        activate_bulk_mode,
        add_to_sync_queue,
        get_bulk_sync_settings,
        is_bulk_mode_active,
        schedule_bulk_sync_processing,
        should_use_bulk_mode,
    )
    from ecommerce_integrations.shopware6.properties import upload_item_properties
    from ecommerce_integrations.shopware6.utils import get_shopware_document_id

    item = doc

    if item.flags.from_integration:
        return

    if getattr(frappe.flags, "skip_shopware_sync", False):
        return

    if not _is_shopware_enabled(check_update_setting=True):
        return

    if not get_shopware_document_id("Item", item.name):
        return

    settings = get_bulk_sync_settings()

    if frappe.flags.in_import or getattr(frappe.flags, "in_bulk_update", False):
        add_to_sync_queue(item.name, "properties")
        if not is_bulk_mode_active():
            activate_bulk_mode()
        return

    if not settings["enabled"]:
        frappe.enqueue(
            "ecommerce_integrations.shopware6.properties.upload_item_properties",
            queue="short",
            doc=doc,
            enqueue_after_commit=True,
        )
        return

    if should_use_bulk_mode():
        add_to_sync_queue(item.name, "properties")
        schedule_bulk_sync_processing()
        return

    upload_item_properties(doc, method)


def queue_item_group_for_sync(doc, method=None):
    """Queue an Item Group for category sync."""
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
    """Queue price sync when an Item Price changes."""
    from ecommerce_integrations.shopware6.bulk_sync import (
        activate_bulk_mode,
        add_to_sync_queue,
        get_bulk_sync_settings,
        is_bulk_mode_active,
        schedule_bulk_sync_processing,
        should_use_bulk_mode,
    )
    from ecommerce_integrations.shopware6.utils import get_logger, get_shopware_document_id

    item_price = doc

    if item_price.flags.from_integration:
        return

    if getattr(frappe.flags, "skip_shopware_sync", False):
        return

    setting = _get_shopware_setting()
    if not setting:
        return

    item_code = item_price.item_code
    if not item_code:
        return

    if not get_shopware_document_id("Item", item_code):
        return

    selling_price_list = setting.get("price_list") or frappe.db.get_single_value(
        "Selling Settings", "selling_price_list"
    )
    if item_price.price_list != selling_price_list:
        return

    get_logger("queue_price_for_sync").info(
        f"Shopware6: Queueing price sync for item {item_code} (price list: {item_price.price_list})"
    )

    settings = get_bulk_sync_settings()

    if frappe.flags.in_import or getattr(frappe.flags, "in_bulk_update", False):
        add_to_sync_queue(item_code, "price")
        if not is_bulk_mode_active():
            activate_bulk_mode()
        return

    if not settings["enabled"]:
        _enqueue_shopware_price_sync(item_code)
        return

    if should_use_bulk_mode():
        add_to_sync_queue(item_code, "price")
        schedule_bulk_sync_processing()
        return

    _enqueue_shopware_price_sync(item_code)


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


def _enqueue_shopware_product_sync(item_code: str) -> None:
    # ``job_id`` + ``deduplicate=True`` collapse rapid double-saves of the
    # same Item into a single queued job. Without this, every save spawns a
    # parallel sync job for the same item_code.
    frappe.enqueue(
        "ecommerce_integrations.shopware6.bulk_sync.sync_single_item_to_shopware",
        queue="short",
        item_code=item_code,
        enqueue_after_commit=True,
        job_id=f"shopware6_sync_item:{item_code}",
        deduplicate=True,
    )


def _enqueue_shopware_price_sync(item_code: str) -> None:
    frappe.enqueue(
        "ecommerce_integrations.shopware6.product_export.update_item_price_in_shopware",
        queue="short",
        item_code=item_code,
        enqueue_after_commit=True,
        job_id=f"shopware6_price:{item_code}",
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
