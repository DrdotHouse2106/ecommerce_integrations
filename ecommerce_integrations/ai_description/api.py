# Copyright (c) 2024, Frappe and contributors
# For license information, please see license.txt

"""
Whitelisted API methods for AI Description Generation

These methods can be called from the frontend (JavaScript) or via REST API.
"""

import frappe
from frappe import _


@frappe.whitelist()
def generate_description_for_item(item_code: str) -> dict:
    """
    Generate AI description for a single item

    Args:
        item_code: ERPNext Item code

    Returns:
        dict with success status and result/error
    """
    from .gemini import generate_description

    try:
        result = generate_description(item_code)
        return result
    except Exception as e:
        frappe.log_error(
            title=f"AI Description API Error: {item_code}",
            message=str(e)
        )
        return {
            "success": False,
            "error": str(e)
        }


@frappe.whitelist()
def generate_descriptions_batch(item_codes: str = None) -> dict:
    """
    Generate AI descriptions for multiple items

    Args:
        item_codes: JSON string of item codes, or None to use filter settings

    Returns:
        dict with batch processing results
    """
    import json
    from .gemini import generate_descriptions_batch as batch_generate
    from .doctype.ai_description_setting.ai_description_setting import get_settings

    settings = get_settings()

    if item_codes:
        # Parse provided item codes
        codes = json.loads(item_codes) if isinstance(item_codes, str) else item_codes
    else:
        # Get items based on filter settings
        codes = get_pending_items(limit=settings.batch_size or 10)

    if not codes:
        return {
            "success": True,
            "message": "No items to process",
            "total": 0
        }

    result = batch_generate(codes)

    # Update settings with processing stats
    settings.reload()
    settings.last_batch_run = frappe.utils.now()
    settings.items_processed = (settings.items_processed or 0) + result["success"]
    settings.save(ignore_permissions=True)

    return result


@frappe.whitelist()
def get_pending_items(limit: int = 50) -> list:
    """
    Get list of items pending AI description generation

    Args:
        limit: Maximum number of items to return

    Returns:
        list of item codes
    """
    from .doctype.ai_description_setting.ai_description_setting import get_settings

    settings = get_settings()
    filters = settings._build_item_filters()

    items = frappe.get_all(
        "Item",
        filters=filters,
        fields=["item_code"],
        limit=int(limit),
        order_by="modified desc"
    )

    return [item.item_code for item in items]


@frappe.whitelist()
def get_generation_status(item_code: str) -> dict:
    """
    Get AI description generation status for an item

    Args:
        item_code: ERPNext Item code

    Returns:
        dict with status information
    """
    item = frappe.get_doc("Item", item_code)

    # Get latest log entry
    logs = frappe.get_all(
        "AI Description Log",
        filters={"item": item_code},
        fields=["status", "generation_time", "model_used", "error_message", "creation"],
        order_by="creation desc",
        limit=1
    )

    return {
        "item_code": item_code,
        "ai_generated": bool(item.get("ai_description_generated")),
        "generation_date": item.get("ai_generation_date"),
        "model_used": item.get("ai_model_used"),
        "reviewed": bool(item.get("ai_description_reviewed")),
        "has_short_description": bool(item.get("ai_short_description")),
        "has_long_description": bool(item.get("ai_long_description")),
        "last_log": logs[0] if logs else None
    }


@frappe.whitelist()
def regenerate_description(item_code: str) -> dict:
    """
    Regenerate AI description for an item (even if already generated)

    Args:
        item_code: ERPNext Item code

    Returns:
        dict with generation result
    """
    # Reset the generated flag first
    frappe.db.set_value("Item", item_code, "ai_description_generated", 0)
    frappe.db.commit()

    return generate_description_for_item(item_code)


@frappe.whitelist()
def mark_as_reviewed(item_code: str, reviewed: bool = True) -> dict:
    """
    Mark an item's AI description as reviewed

    Args:
        item_code: ERPNext Item code
        reviewed: Whether the description is reviewed (default: True)

    Returns:
        dict with success status
    """
    try:
        frappe.db.set_value("Item", item_code, "ai_description_reviewed", 1 if reviewed else 0)
        frappe.db.commit()
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}


@frappe.whitelist()
def get_ai_settings_summary() -> dict:
    """
    Get summary of AI Description settings and status

    Returns:
        dict with settings summary
    """
    from .doctype.ai_description_setting.ai_description_setting import get_settings

    settings = get_settings()

    # Count items with AI descriptions
    total_with_ai = frappe.db.count("Item", {"ai_description_generated": 1})
    total_reviewed = frappe.db.count("Item", {"ai_description_reviewed": 1})
    total_pending = frappe.db.count("Item", {
        "ai_description_generated": 0,
        "disabled": 0,
        "has_variants": 0
    })

    return {
        "enabled": settings.enabled,
        "model": settings.gemini_model,
        "batch_enabled": settings.enable_batch_processing,
        "batch_size": settings.batch_size,
        "stats": {
            "total_with_ai": total_with_ai,
            "total_reviewed": total_reviewed,
            "total_pending": total_pending,
            "items_processed": settings.items_processed,
            "last_batch_run": settings.last_batch_run
        }
    }


@frappe.whitelist()
def copy_ai_to_main_description(item_code: str) -> dict:
    """
    Copy AI-generated description to the main description field

    Args:
        item_code: ERPNext Item code

    Returns:
        dict with success status
    """
    try:
        item = frappe.get_doc("Item", item_code)

        if not item.ai_long_description:
            return {"success": False, "error": "No AI description available"}

        item.description = item.ai_long_description
        item.save(ignore_permissions=True)

        return {"success": True, "message": "Description copied successfully"}
    except Exception as e:
        return {"success": False, "error": str(e)}


@frappe.whitelist()
def sync_ai_description_to_shopware(item_code: str) -> dict:
    """
    Sync AI-generated description to Shopware

    This copies the AI description to the standard fields that
    Shopware sync uses, then triggers a sync.

    Args:
        item_code: ERPNext Item code

    Returns:
        dict with sync result
    """
    try:
        item = frappe.get_doc("Item", item_code)

        # Copy AI fields to standard/Shopware fields
        if item.ai_long_description:
            item.description = item.ai_long_description

        if item.ai_seo_title:
            if hasattr(item, 'seo_title'):
                item.seo_title = item.ai_seo_title

        if item.ai_seo_description:
            if hasattr(item, 'seo_meta_description'):
                item.seo_meta_description = item.ai_seo_description

        item.save(ignore_permissions=True)

        # Trigger Shopware sync if available
        try:
            from ..shopware6.product_export import sync_item_to_shopware
            sync_result = sync_item_to_shopware(item_code)
            return {
                "success": True,
                "message": "Description synced to Shopware",
                "sync_result": sync_result
            }
        except ImportError:
            return {
                "success": True,
                "message": "Description copied. Shopware sync not available."
            }

    except Exception as e:
        frappe.log_error(
            title=f"AI to Shopware Sync Error: {item_code}",
            message=str(e)
        )
        return {"success": False, "error": str(e)}
