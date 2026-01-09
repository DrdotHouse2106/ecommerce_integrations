"""
Shopware 6 Category Handler

Manages category synchronization from ERPNext Item Groups to Shopware categories.
Supports hierarchy sync, FAQ custom fields, SEO fields, and category images.
"""

import hashlib
import mimetypes
import os
import time
from typing import Any, Dict, List, Optional

import frappe
import requests
from frappe.utils import get_files_path

from ecommerce_integrations.shopware6.connection import temp_shopware_session, get_shopware_client
from ecommerce_integrations.shopware6.constants import (
    SHOPWARE_CATEGORY_CUSTOM_FIELD_SET_NAME,
    CATEGORY_FAQ_FIELDS_MAP,
)
from ecommerce_integrations.shopware6.base.cache_manager import get_cache
from ecommerce_integrations.shopware6.export.utils import (
    generate_uuid,
    sanitize_filename,
    get_item_group_hierarchy,
)
from ecommerce_integrations.shopware6.utils import get_logger, create_shopware_log


def ensure_category_custom_field_set(client) -> Optional[str]:
    """
    Ensure the ERPNext category custom field set exists in Shopware.

    Creates a custom field set with fields for FAQ questions and answers.

    Args:
        client: Shopware API client

    Returns:
        Custom field set ID if successful, None otherwise
    """
    cache = get_cache()
    cached_id = cache.get("category_custom_field_set", SHOPWARE_CATEGORY_CUSTOM_FIELD_SET_NAME)
    if cached_id:
        return cached_id

    try:
        # Search for existing
        response = client.request_post(
            "search/custom-field-set",
            {"filter": [{"type": "equals", "field": "name", "value": SHOPWARE_CATEGORY_CUSTOM_FIELD_SET_NAME}]}
        )
        sets = response.get("data", [])

        if sets:
            set_id = sets[0]["id"]
            cache.set("category_custom_field_set", SHOPWARE_CATEGORY_CUSTOM_FIELD_SET_NAME, set_id)
            return set_id

        # Build FAQ custom fields
        set_id = generate_uuid(f"custom_field_set_{SHOPWARE_CATEGORY_CUSTOM_FIELD_SET_NAME}")
        custom_fields = []
        position = 1

        for i in range(1, 6):
            # Question field
            q_field_name = f"erpnext_faq{i}_question"
            custom_fields.append({
                "id": generate_uuid(f"custom_field_{q_field_name}"),
                "name": q_field_name,
                "type": "text",
                "config": {
                    "label": {"de-DE": f"FAQ {i} - Frage", "en-GB": f"FAQ {i} - Question"},
                    "customFieldPosition": position,
                },
            })
            position += 1

            # Answer field
            a_field_name = f"erpnext_faq{i}_answer"
            custom_fields.append({
                "id": generate_uuid(f"custom_field_{a_field_name}"),
                "name": a_field_name,
                "type": "html",
                "config": {
                    "label": {"de-DE": f"FAQ {i} - Antwort", "en-GB": f"FAQ {i} - Answer"},
                    "customFieldPosition": position,
                    "componentName": "sw-text-editor",
                },
            })
            position += 1

        payload = {
            "id": set_id,
            "name": SHOPWARE_CATEGORY_CUSTOM_FIELD_SET_NAME,
            "config": {
                "label": {"de-DE": "ERPNext Kategorie-Felder", "en-GB": "ERPNext Category Fields"},
                "translated": True,
            },
            "customFields": custom_fields,
            "relations": [{
                "id": generate_uuid(f"relation_{SHOPWARE_CATEGORY_CUSTOM_FIELD_SET_NAME}_category"),
                "entityName": "category",
            }],
        }

        client.request_post("custom-field-set", payload)
        cache.set("category_custom_field_set", SHOPWARE_CATEGORY_CUSTOM_FIELD_SET_NAME, set_id)
        return set_id

    except BaseException as e:
        get_logger().error("Failed to ensure category custom field set", persist=False)
        return None


def get_item_group_data(item_group_name: str) -> Optional[Dict[str, Any]]:
    """
    Get Item Group data including description, FAQ, SEO, image, and Shopware active status.

    Args:
        item_group_name: Name of the Item Group

    Returns:
        Dict with Item Group data or None
    """
    try:
        item_group = frappe.get_doc("Item Group", item_group_name)

        # shopware_active: 1 = active (default), 0 = inactive in Shopware only
        shopware_active = getattr(item_group, "shopware_active", None)
        if shopware_active is None:
            shopware_active = True
        else:
            shopware_active = bool(shopware_active)

        return {
            "name": item_group.name,
            "item_group_name": item_group.item_group_name,
            "description": getattr(item_group, "description", None),
            "image": item_group.image,
            "category_image": getattr(item_group, "category_image", None),
            "seo_title": getattr(item_group, "seo_title", None),
            "seo_meta_description": getattr(item_group, "seo_meta_description", None),
            "seo_keywords": getattr(item_group, "seo_keywords", None),
            "shopware_active": shopware_active,
            "faq1_question": getattr(item_group, "faq1_question", None),
            "faq1_answer": getattr(item_group, "faq1_answer", None),
            "faq2_question": getattr(item_group, "faq2_question", None),
            "faq2_answer": getattr(item_group, "faq2_answer", None),
            "faq3_question": getattr(item_group, "faq3_question", None),
            "faq3_answer": getattr(item_group, "faq3_answer", None),
            "faq4_question": getattr(item_group, "faq4_question", None),
            "faq4_answer": getattr(item_group, "faq4_answer", None),
            "faq5_question": getattr(item_group, "faq5_question", None),
            "faq5_answer": getattr(item_group, "faq5_answer", None),
        }
    except BaseException:
        return None


def build_category_custom_fields(item_group_data: Dict[str, Any]) -> Dict[str, str]:
    """
    Build custom fields dict for category from Item Group data.

    Args:
        item_group_data: Dict with Item Group FAQ fields

    Returns:
        Dict mapping Shopware custom field names to values
    """
    custom_fields = {}
    for erpnext_field, shopware_field in CATEGORY_FAQ_FIELDS_MAP.items():
        value = item_group_data.get(erpnext_field)
        if value:
            custom_fields[shopware_field] = value
    return custom_fields


def get_or_create_media_folder(client, folder_name: str) -> Optional[str]:
    """
    Get or create a media folder in Shopware by name.

    Args:
        client: Shopware API client
        folder_name: Name of the media folder

    Returns:
        Folder ID if successful, None otherwise
    """
    cache = get_cache()
    cache_key = folder_name.lower().replace(" ", "_")
    cached_id = cache.get("media_folder", cache_key)
    if cached_id:
        return cached_id

    try:
        # Search for existing
        response = client.request_post(
            "search/media-folder",
            {"filter": [{"type": "equals", "field": "name", "value": folder_name}]}
        )
        folders = response.get("data", [])

        if folders:
            folder_id = folders[0]["id"]
            cache.set("media_folder", cache_key, folder_id)
            return folder_id

        # Create new
        folder_id = generate_uuid(f"media_folder_{folder_name}")
        config_response = client.request_post("search/media-folder-configuration", {"limit": 1})
        configs = config_response.get("data", [])
        config_id = configs[0]["id"] if configs else None

        folder_payload = {"id": folder_id, "name": folder_name}
        if config_id:
            folder_payload["configurationId"] = config_id
        else:
            folder_payload["configuration"] = {
                "createThumbnails": True,
                "keepAspectRatio": True,
                "thumbnailQuality": 80,
            }

        try:
            client.request_post("media-folder", folder_payload)
            cache.set("media_folder", cache_key, folder_id)
            return folder_id
        except BaseException as e:
            error_str = str(e).lower()
            if "already exists" in error_str or "duplicate" in error_str:
                response = client.request_post(
                    "search/media-folder",
                    {"filter": [{"type": "equals", "field": "name", "value": folder_name}]}
                )
                folders = response.get("data", [])
                if folders:
                    folder_id = folders[0]["id"]
                    cache.set("media_folder", cache_key, folder_id)
                    return folder_id
            get_logger().error(f"Failed to create media folder '{folder_name}'", persist=False)
            return None

    except BaseException as e:
        get_logger().error(f"Failed to get or create media folder '{folder_name}': {e}", persist=False)
        return None


def get_image_content_and_filename(image_path: str) -> tuple:
    """
    Get image content and filename from a path (local or URL).

    Args:
        image_path: Local file path or URL (supports /file/... external storage URLs)

    Returns:
        Tuple of (image_content, filename, extension) or (None, None, None) on error
    """
    try:
        if image_path.startswith("http"):
            response = requests.get(image_path, timeout=30)
            if response.status_code != 200:
                return None, None, None
            image_content = response.content
            filename = image_path.split("/")[-1].split("?")[0]
        elif image_path.startswith("/file/"):
            # External storage URL - load via Frappe File API
            # Path format: /file/{file_id}/{filename}
            parts = image_path.split("/")
            if len(parts) >= 3:
                file_id = parts[2]  # e.g. "392be7e0e5"
                try:
                    file_doc = frappe.get_doc("File", file_id)
                    image_content = file_doc.get_content()
                    filename = parts[-1].split("?")[0]
                except Exception:
                    return None, None, None
            else:
                return None, None, None
        else:
            if image_path.startswith("/files/"):
                full_path = get_files_path() + image_path[6:]
            elif image_path.startswith("/private/files/"):
                full_path = get_files_path(is_private=True) + image_path[14:]
            else:
                full_path = image_path

            if not os.path.exists(full_path):
                return None, None, None

            with open(full_path, "rb") as f:
                image_content = f.read()
            filename = sanitize_filename(os.path.basename(full_path))

        # Extract extension
        ext = filename.split(".")[-1].lower() if "." in filename else "jpg"
        if ext not in ["jpg", "jpeg", "png", "gif", "webp"]:
            ext = "jpg"

        # Remove extension from filename (Shopware appends it from 'extension' parameter)
        filename_without_ext = filename.rsplit(".", 1)[0] if "." in filename else filename

        return image_content, filename_without_ext, ext

    except Exception as e:
        get_logger().error(f"Failed to get image content from {image_path}: {e}", persist=False)
        return None, None, None


def upload_category_media(client, category_id: str, image_path: str) -> Optional[str]:
    """
    Upload and assign media to a category in Shopware.

    Args:
        client: Shopware API client
        category_id: Shopware category ID
        image_path: Path to the image file

    Returns:
        Media ID if successful, None otherwise
    """
    try:
        folder_id = get_or_create_media_folder(client, "Category Media")
        if not folder_id:
            return None

        media_id = generate_uuid(f"category_media_{category_id}")

        # Get image content using helper
        image_content, filename_without_ext, ext = get_image_content_and_filename(image_path)
        if not image_content:
            return None

        # Use unique filename to avoid CONTENT__MEDIA_DUPLICATED_FILE_NAME error
        unique_filename = f"{filename_without_ext}-{int(time.time())}"

        # Check if media exists and has valid content
        media_exists = False
        media_has_content = False
        try:
            existing = client.request_get(f"media/{media_id}")
            if existing and existing.get("data"):
                media_exists = True
                # Check if media has actual content (fileName indicates uploaded)
                media_has_content = bool(existing.get("data", {}).get("fileName"))
        except BaseException:
            pass

        # If media exists with valid content, return it
        if media_exists and media_has_content:
            return media_id

        if not media_exists:
            try:
                client.request_post("media", {"id": media_id, "mediaFolderId": folder_id})
            except BaseException as e:
                error_str = str(e).lower()
                if "already exists" not in error_str and "duplicate" not in error_str:
                    return None

        # Upload content with unique filename
        try:
            client.request_post(
                f"_action/media/{media_id}/upload",
                payload=image_content,
                content_type="octet-stream",
                additional_query_params={"extension": ext, "fileName": unique_filename}
            )
        except BaseException as e:
            error_str = str(e).lower()
            if "already exists" not in error_str and "duplicate" not in error_str:
                return None

        return media_id

    except BaseException as e:
        get_logger().error("Failed to upload category media", persist=False)
        return None


def get_or_create_category(
    client,
    category_name: str,
    parent_id: str = None,
    item_group_data: Dict[str, Any] = None
) -> Optional[str]:
    """
    Get existing or create/update Category in Shopware.

    Supports FAQ fields, SEO fields, and category images.

    Args:
        client: Shopware API client
        category_name: Name of the category
        parent_id: Parent category ID
        item_group_data: ERPNext Item Group data

    Returns:
        Shopware category ID if successful, None otherwise
    """
    cache = get_cache()

    try:
        # Search for existing
        response = client.request_post(
            "search/category",
            {"filter": [{"type": "equals", "field": "name", "value": category_name}]}
        )
        categories = response.get("data", [])

        existing_cat_id = None
        if categories:
            existing_cat_id = categories[0]["id"]
            cache.set_category_id(category_name, existing_cat_id)

        # Get root category if no parent
        if not parent_id and not existing_cat_id:
            root_resp = client.request_post(
                "search/category",
                {"filter": [{"type": "equals", "field": "parentId", "value": None}], "limit": 1}
            )
            root_cats = root_resp.get("data", [])
            if root_cats:
                parent_id = root_cats[0]["id"]

        cat_id = existing_cat_id or generate_uuid(f"category_{category_name}")

        is_active = True
        if item_group_data and "shopware_active" in item_group_data:
            is_active = item_group_data["shopware_active"]

        payload = {
            "id": cat_id,
            "name": category_name,
            "active": is_active,
            "displayNestedProducts": True,
        }

        if parent_id:
            payload["parentId"] = parent_id

        if item_group_data:
            if item_group_data.get("description"):
                payload["description"] = item_group_data["description"]
            if item_group_data.get("seo_title"):
                payload["metaTitle"] = item_group_data["seo_title"]
            if item_group_data.get("seo_meta_description"):
                payload["metaDescription"] = item_group_data["seo_meta_description"]
            if item_group_data.get("seo_keywords"):
                payload["keywords"] = item_group_data["seo_keywords"]

            custom_fields = build_category_custom_fields(item_group_data)
            if custom_fields:
                ensure_category_custom_field_set(client)
                payload["customFields"] = custom_fields

        # Create or update
        if existing_cat_id:
            client.request_patch(f"category/{cat_id}", payload)
        else:
            client.request_post("category", payload)

        cache.set_category_id(category_name, cat_id)

        # Handle category image
        if item_group_data:
            image_path = item_group_data.get("category_image") or item_group_data.get("image")
            if image_path:
                media_id = upload_category_media(client, cat_id, image_path)
                if media_id:
                    try:
                        client.request_patch(f"category/{cat_id}", {"mediaId": media_id})
                    except Exception:
                        pass

        return cat_id

    except BaseException as e:
        get_logger().error(f"Failed to get/create Category {category_name}: {e}", persist=False)
        return None


def sync_category_hierarchy(client, item_group_name: str) -> Optional[str]:
    """
    Sync the full category hierarchy from ERPNext Item Group to Shopware.

    Creates all parent categories if they don't exist, maintaining the hierarchy.

    Args:
        client: Shopware API client
        item_group_name: ERPNext Item Group name

    Returns:
        Shopware category ID of the leaf category
    """
    hierarchy = get_item_group_hierarchy(item_group_name)
    if not hierarchy:
        return None

    # Skip root categories
    root_to_skip = ["All Item Groups", "Alle Artikelgruppen"]
    while hierarchy and hierarchy[0] in root_to_skip:
        hierarchy = hierarchy[1:]

    if not hierarchy:
        return None

    # Get Shopware root
    root_resp = client.request_post(
        "search/category",
        {"filter": [{"type": "equals", "field": "parentId", "value": None}], "limit": 1}
    )
    root_cats = root_resp.get("data", [])
    parent_id = root_cats[0]["id"] if root_cats else None

    # Create each level
    last_category_id = None
    for category_name in hierarchy:
        item_group_data = get_item_group_data(category_name)
        category_id = get_or_create_category(client, category_name, parent_id, item_group_data)
        if category_id:
            parent_id = category_id
            last_category_id = category_id

    return last_category_id


def get_all_item_categories(item_code: str, include_variant_categories: bool = True) -> List[str]:
    """
    Get all categories for an item, including multi-category assignments.

    Supports:
    1. Standard ERPNext: item.item_group
    2. Webshop App: Website Item's website_item_groups
    3. Variant categories: Categories from variants

    Args:
        item_code: ERPNext Item code
        include_variant_categories: Include categories from variants

    Returns:
        List of unique Item Group names
    """
    categories = []

    try:
        item = frappe.get_doc("Item", item_code)
        if item.item_group:
            categories.append(item.item_group)

        # Check Webshop app
        if frappe.db.exists("DocType", "Website Item"):
            website_item_name = frappe.db.get_value(
                "Website Item", {"item_code": item_code}, "name"
            )

            if website_item_name:
                groups = frappe.get_all(
                    "Website Item Group",
                    filters={"parent": website_item_name, "parenttype": "Website Item"},
                    fields=["item_group"]
                )
                for g in groups:
                    if g.item_group and g.item_group not in categories:
                        categories.append(g.item_group)

            # Include variant categories
            if include_variant_categories and item.has_variants:
                variant_codes = frappe.get_all(
                    "Item", filters={"variant_of": item_code}, pluck="name"
                )
                for vc in variant_codes:
                    vwi = frappe.db.get_value("Website Item", {"item_code": vc}, "name")
                    if vwi:
                        vgroups = frappe.get_all(
                            "Website Item Group",
                            filters={"parent": vwi, "parenttype": "Website Item"},
                            fields=["item_group"]
                        )
                        for vg in vgroups:
                            if vg.item_group and vg.item_group not in categories:
                                categories.append(vg.item_group)

    except Exception as e:
        get_logger().error(f"Error getting categories for {item_code}: {e}", persist=False)

    return categories


def sync_all_item_categories(client, item_code: str) -> List[Dict[str, str]]:
    """
    Sync all categories for an item to Shopware.

    Args:
        client: Shopware API client
        item_code: ERPNext Item code

    Returns:
        List of category ID dicts for Shopware payload
    """
    categories = get_all_item_categories(item_code)
    category_ids = []
    seen_ids = set()

    for item_group_name in categories:
        try:
            category_id = sync_category_hierarchy(client, item_group_name)
            if category_id and category_id not in seen_ids:
                seen_ids.add(category_id)
                category_ids.append({"id": category_id})
        except Exception as e:
            get_logger().error(f"Error syncing category {item_group_name} for {item_code}: {e}", persist=False)

    return category_ids


def clear_product_categories(client, product_id: str) -> bool:
    """
    Clear all category assignments from a product in Shopware.

    Uses Shopware Sync API with criteria-based delete which is:
    - Faster than individual DELETE requests
    - More reliable than payload-based delete (avoids Cardinality violation 1242)

    Reference: https://forum.shopware.com/t/produkt-kategoriezuweiesung-ueber-api-loeschen/99563

    Args:
        client: Shopware API client
        product_id: Shopware product UUID

    Returns:
        True if successful
    """
    try:
        # Use criteria-based delete - deletes all product_category entries for this product
        # This avoids the "Cardinality violation 1242" SQL error that happens with payload-based delete
        sync_payload = {
            "clear-product-categories": {
                "entity": "product_category",
                "action": "delete",
                "criteria": [
                    {
                        "type": "equals",
                        "field": "productId",
                        "value": product_id
                    }
                ]
            }
        }

        result = client.request_post("_action/sync", sync_payload)
        frappe.logger().debug(f"Cleared categories from product {product_id}")
        return True

    except Exception as e:
        error_str = str(e)
        # If criteria-based delete fails, fall back to individual deletes
        if "404" not in error_str and "not found" not in error_str.lower():
            frappe.logger().warning(f"Criteria-based category clear failed for {product_id}, trying fallback: {e}")
            return _clear_product_categories_fallback(client, product_id)
        return True


def _clear_product_categories_fallback(client, product_id: str) -> bool:
    """Fallback: Clear categories using individual DELETE requests."""
    try:
        response = client.request_get(f"product/{product_id}?associations[categories][]=")
        product_data = response.get("data", {})
        categories = product_data.get("categories", [])

        if not categories:
            return True

        for cat in categories:
            cat_id = cat.get("id")
            if cat_id:
                try:
                    client.request_delete(f"product/{product_id}/categories/{cat_id}")
                except Exception:
                    pass  # Ignore individual delete errors

        return True
    except Exception as e:
        frappe.logger().error(f"Fallback category clear failed for {product_id}: {e}")
        return False


@frappe.whitelist()
@temp_shopware_session
def debug_get_item_categories(client, item_code: str) -> dict:
    """Debug function to see what categories would be synced for an item."""
    from ecommerce_integrations.shopware6.export.category_handler import get_all_item_categories, sync_category_hierarchy

    result = {
        "item_code": item_code,
        "erpnext_categories": [],
        "shopware_category_ids": []
    }

    try:
        item = frappe.get_doc("Item", item_code)
        result["item_group"] = item.item_group

        all_cats = get_all_item_categories(item_code)
        result["erpnext_categories"] = all_cats

        for cat_name in all_cats:
            cat_id = sync_category_hierarchy(client, cat_name)
            result["shopware_category_ids"].append({
                "name": cat_name,
                "shopware_id": cat_id
            })
    except Exception as e:
        result["error"] = str(e)

    return result


@frappe.whitelist()
@temp_shopware_session
def debug_clear_product_categories(client, product_id: str) -> dict:
    """
    Debug function to test clearing product categories.
    Returns detailed information about what happened.
    """
    result = {
        "product_id": product_id,
        "categories_before": [],
        "delete_operations": [],
        "sync_response": None,
        "categories_after": [],
        "success": False,
        "error": None
    }

    try:
        # Get current categories
        response = client.request_get(f"product/{product_id}?associations[categories][]=")
        product_data = response.get("data", {})
        categories = product_data.get("categories", [])

        result["categories_before"] = [
            {"id": c.get("id"), "name": c.get("translated", {}).get("name", c.get("name"))}
            for c in categories
        ]

        if not categories:
            result["success"] = True
            result["message"] = "No categories to clear"
            return result

        # Build delete operations
        delete_operations = []
        for cat in categories:
            cat_id = cat.get("id")
            if cat_id:
                delete_operations.append({
                    "productId": product_id,
                    "categoryId": cat_id
                })

        result["delete_operations"] = delete_operations

        # Try sync endpoint
        sync_payload = {
            "delete-product_category": {
                "entity": "product_category",
                "action": "delete",
                "payload": delete_operations
            }
        }

        try:
            sync_result = client.request_post("_action/sync", sync_payload)
            result["sync_response"] = sync_result
        except Exception as e:
            result["sync_error"] = str(e)
            # Try fallback
            for cat in categories:
                cat_id = cat.get("id")
                if cat_id:
                    try:
                        client.request_delete(f"product/{product_id}/categories/{cat_id}")
                    except Exception as del_err:
                        result["fallback_errors"] = result.get("fallback_errors", [])
                        result["fallback_errors"].append(str(del_err))

        # Check categories after
        response = client.request_get(f"product/{product_id}?associations[categories][]=")
        product_data = response.get("data", {})
        categories_after = product_data.get("categories", [])

        result["categories_after"] = [
            {"id": c.get("id"), "name": c.get("translated", {}).get("name", c.get("name"))}
            for c in categories_after
        ]

        result["success"] = len(categories_after) == 0

    except Exception as e:
        result["error"] = str(e)

    return result


@frappe.whitelist()
@temp_shopware_session
def force_resync_category_image(client, item_group_name: str) -> bool:
    """
    Force resync category image by deleting old media and creating fresh one.

    Args:
        client: Shopware API client
        item_group_name: ERPNext Item Group name

    Returns:
        True if successful
    """
    try:
        # Find category in Shopware
        response = client.request_post(
            "search/category",
            {"filter": [{"type": "equals", "field": "name", "value": item_group_name}]}
        )
        categories = response.get("data", [])

        if not categories:
            get_logger().error("Category not found in Shopware", persist=False)
            return False

        cat = categories[0]
        cat_id = cat["id"]
        old_media_id = cat.get("mediaId")

        # Remove media reference from category first
        if old_media_id:
            try:
                client.request_patch(f"category/{cat_id}", {"mediaId": None})
                frappe.logger().info(f"Removed old media reference from category {item_group_name}")
            except Exception as e:
                frappe.logger().warning(f"Could not remove media reference: {e}")

            # Delete old media
            try:
                client.request_delete(f"media/{old_media_id}")
                frappe.logger().info(f"Deleted old media {old_media_id}")
            except Exception as e:
                frappe.logger().warning(f"Could not delete old media: {e}")

        # Also search for and delete any media with matching filename (to avoid duplicates)
        try:
            search_resp = client.request_post("search/media", {
                "filter": [{"type": "contains", "field": "fileName", "value": item_group_name}]
            })
            existing_media = search_resp.get("data", [])
            for media in existing_media:
                media_id_to_delete = media.get("id")
                if media_id_to_delete:
                    try:
                        client.request_delete(f"media/{media_id_to_delete}")
                        frappe.logger().info(f"Deleted duplicate media {media_id_to_delete}")
                    except Exception:
                        pass
        except Exception as e:
            frappe.logger().warning(f"Could not search for duplicate media: {e}")

        # Get Item Group data
        item_group_data = get_item_group_data(item_group_name)
        if not item_group_data:
            return False

        image_path = item_group_data.get("category_image") or item_group_data.get("image")
        if not image_path:
            return False

        # Get image content using helper
        image_content, filename_without_ext, ext = get_image_content_and_filename(image_path)
        if not image_content:
            get_logger().error("Could not load image", persist=False)
            return False

        # Upload new media with fresh UUID (not deterministic)
        folder_id = get_or_create_media_folder(client, "Category Media")
        if not folder_id:
            return False

        timestamp = int(time.time())
        fresh_media_id = hashlib.md5(f"category_media_{cat_id}_{timestamp}".encode()).hexdigest()
        # Use unique filename to avoid CONTENT__MEDIA_DUPLICATED_FILE_NAME error
        unique_filename = f"{filename_without_ext}-{timestamp}"

        # Create new media
        try:
            client.request_post("media", {"id": fresh_media_id, "mediaFolderId": folder_id})
        except Exception as e:
            get_logger().error("Failed to create media", persist=False)
            return False

        # Upload content
        try:
            client.request_post(
                f"_action/media/{fresh_media_id}/upload",
                payload=image_content,
                content_type="octet-stream",
                additional_query_params={"extension": ext, "fileName": unique_filename}
            )
        except Exception as e:
            get_logger().error("Failed to upload media content", persist=False)
            return False

        # Assign to category
        try:
            client.request_patch(f"category/{cat_id}", {"mediaId": fresh_media_id})
            frappe.logger().info(f"Assigned new media {fresh_media_id} to category {item_group_name}")
        except Exception as e:
            get_logger().error("Failed to assign media to category", persist=False)
            return False

        return True

    except Exception as e:
        get_logger().error(f"Error force resyncing category image for {item_group_name}: {e}", persist=False)
        return False


@temp_shopware_session
def sync_item_group_to_shopware(client, item_group_name: str) -> bool:
    """
    Sync an ERPNext Item Group to Shopware as a category.

    This function is typically called from doc_events when an Item Group
    is updated. It syncs the full hierarchy and updates category data.

    Args:
        client: Shopware API client
        item_group_name: ERPNext Item Group name

    Returns:
        True if successful, False otherwise
    """
    try:
        # Skip root categories
        root_to_skip = ["All Item Groups", "Alle Artikelgruppen"]
        if item_group_name in root_to_skip:
            return True

        # Get Item Group data
        item_group_data = get_item_group_data(item_group_name)
        if not item_group_data:
            get_logger().error("Item Group not found", persist=False)
            return False

        # Sync the category hierarchy
        category_id = sync_category_hierarchy(client, item_group_name)

        if category_id:
            frappe.logger("shopware6").info(
                f"Synced Item Group '{item_group_name}' to Shopware category {category_id}"
            )
            return True
        else:
            get_logger().error(f"Failed to sync Item Group '{item_group_name}' to Shopware", persist=False)
            return False

    except Exception as e:
        get_logger().error(f"Error syncing Item Group '{item_group_name}': {e}", persist=False)
        return False
