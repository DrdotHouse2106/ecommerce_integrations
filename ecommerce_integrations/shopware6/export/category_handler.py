"""
Shopware 6 Category Handler

Manages category synchronization from ERPNext Item Groups to Shopware categories.
Supports hierarchy sync, FAQ custom fields, SEO fields, and category images.
"""

import hashlib
import os
import time
from typing import Any

import frappe
import requests
from frappe.utils import get_files_path

# CRITICAL: ShopwareAPIError inherits from BaseException, NOT Exception!
from lib_shopware6_api_base.conf_shopware6_api_base_classes import ShopwareAPIError

from ecommerce_integrations.shopware6.base.cache_manager import get_cache
from ecommerce_integrations.shopware6.connection import temp_shopware_session
from ecommerce_integrations.shopware6.constants import (
    CATEGORY_FAQ_FIELDS_MAP,
    ROOT_ITEM_GROUPS,
    SHOPWARE_CATEGORY_CUSTOM_FIELD_SET_NAME,
    SHOPWARE_CATEGORY_PRIORITY,
)
from ecommerce_integrations.shopware6.export.utils import (
    generate_uuid,
    get_item_group_hierarchy,
    sanitize_filename,
)
from ecommerce_integrations.shopware6.utils import create_shopware_log, get_logger


def ensure_category_custom_field_set(client) -> str | None:
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
        sets = response.data or []

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

        # Priority field for frontend sorting
        custom_fields.append({
            "id": generate_uuid(f"custom_field_{SHOPWARE_CATEGORY_PRIORITY}"),
            "name": SHOPWARE_CATEGORY_PRIORITY,
            "type": "int",
            "config": {
                "label": {"de-DE": "Sortier-Priorität", "en-GB": "Sort Priority"},
                "customFieldPosition": position,
                "helpText": {"de-DE": "Niedrigere Zahlen erscheinen zuerst", "en-GB": "Lower numbers appear first"},
            },
        })

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

    except BaseException:
        get_logger().error("Failed to ensure category custom field set", persist=False)
        return None


def get_item_group_data(item_group_name: str) -> dict[str, Any] | None:
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

        # Get priority (default to 0 if not set)
        shopware_priority = getattr(item_group, "shopware_priority", None)
        if shopware_priority is None:
            shopware_priority = 0

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
            "shopware_priority": shopware_priority,
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


def build_category_custom_fields(item_group_data: dict[str, Any]) -> dict[str, Any]:
    """
    Build custom fields dict for category from Item Group data.

    Args:
        item_group_data: Dict with Item Group FAQ and priority fields

    Returns:
        Dict mapping Shopware custom field names to values
    """
    custom_fields = {}

    # FAQ fields
    for erpnext_field, shopware_field in CATEGORY_FAQ_FIELDS_MAP.items():
        value = item_group_data.get(erpnext_field)
        if value:
            custom_fields[shopware_field] = value

    # Priority field (always include, even if 0)
    priority = item_group_data.get("shopware_priority")
    if priority is not None:
        custom_fields[SHOPWARE_CATEGORY_PRIORITY] = int(priority)

    return custom_fields


def get_or_create_media_folder(client, folder_name: str) -> str | None:
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
        folders = response.data or []

        if folders:
            folder_id = folders[0]["id"]
            cache.set("media_folder", cache_key, folder_id)
            return folder_id

        # Create new
        folder_id = generate_uuid(f"media_folder_{folder_name}")
        config_response = client.request_post("search/media-folder-configuration", {"limit": 1})
        configs = config_response.data or []
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
                folders = response.data or []
                if folders:
                    folder_id = folders[0]["id"]
                    cache.set("media_folder", cache_key, folder_id)
                    return folder_id
            get_logger().error(f"Failed to create media folder '{folder_name}'", persist=False)
            return None

    except BaseException as e:
        get_logger().error(f"Failed to get or create media folder '{folder_name}': {e}", persist=False)
        return None


def resolve_category_image_path(item_group_data: dict[str, Any]) -> str | None:
    """
    Get the best available image path from item group data.

    Prefers category_image, falls back to image. Validates that the
    referenced file actually exists before returning.

    Args:
        item_group_data: Dict with 'category_image' and 'image' keys

    Returns:
        Valid image path or None
    """
    if not item_group_data:
        return None

    candidates = [
        item_group_data.get("category_image"),
        item_group_data.get("image"),
    ]

    for path in candidates:
        if not path:
            continue

        # Validate /file/{id}/... paths by checking if the File doc exists
        if path.startswith("/file/"):
            parts = path.split("/")
            if len(parts) >= 3:
                file_id = parts[2]
                if not frappe.db.exists("File", file_id):
                    get_logger().warning(
                        f"Category image file reference broken: {path} "
                        f"(File {file_id} not found), trying fallback",
                        persist=False
                    )
                    continue
            else:
                continue

        return path

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


def upload_category_media(client, category_id: str, image_path: str) -> str | None:
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
        # Sanitize for Shopware (no &, etc.)
        safe_name = filename_without_ext.replace("&", "und").replace("/", "-").replace("\\", "-")
        unique_filename = f"{safe_name}-{int(time.time())}"

        # Check if media exists and has valid content
        media_exists = False
        media_has_content = False
        existing_file_name = None
        try:
            existing = client.request_get(f"media/{media_id}")
            if existing and existing.data:
                media_exists = True
                existing_file_name = (existing.data or {}).get("fileName")
                media_has_content = bool(existing_file_name)
        except BaseException:
            pass

        # If media exists with content, check if image actually changed
        # by comparing the base filename (without timestamp suffix)
        if media_exists and media_has_content:
            # Extract base name from existing (format: "basename-timestamp")
            # and compare with new image's base name
            existing_base = existing_file_name.rsplit("-", 1)[0] if existing_file_name else ""
            if existing_base == safe_name:
                # Same image file - no update needed
                return media_id
            # Image changed - delete old media so we can re-upload
            try:
                client.request_patch(f"category/{category_id}", {"mediaId": None})
            except BaseException:
                pass
            try:
                client.request_delete(f"media/{media_id}")
                media_exists = False
            except BaseException:
                # If delete fails, use a fresh media ID
                media_id = hashlib.md5(
                    f"category_media_{category_id}_{int(time.time())}".encode()
                ).hexdigest()
                media_exists = False

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

    except BaseException:
        get_logger().error("Failed to upload category media", persist=False)
        return None


def get_or_create_category(
    client,
    category_name: str,
    parent_id: str | None = None,
    item_group_data: dict[str, Any] | None = None
) -> str | None:
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
        categories = response.data or []

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
            root_cats = root_resp.data or []
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
            # Log category update
            create_shopware_log(
                status="Success",
                method="category_sync",
                message=f"Updated category: {category_name} -> {cat_id}",
                make_new=True
            )
        else:
            client.request_post("category", payload)
            # Log category creation
            create_shopware_log(
                status="Success",
                method="category_sync",
                message=f"Created category: {category_name} -> {cat_id}",
                make_new=True
            )

        cache.set_category_id(category_name, cat_id)

        # Handle category image
        if item_group_data:
            image_path = resolve_category_image_path(item_group_data)
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
        # Log category error
        create_shopware_log(
            status="Error",
            method="category_sync",
            message=f"Failed to sync category: {category_name}",
            exception=str(e),
            make_new=True
        )
        return None


def sync_category_hierarchy(client, item_group_name: str) -> str | None:
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
    root_to_skip = ROOT_ITEM_GROUPS
    while hierarchy and hierarchy[0] in root_to_skip:
        hierarchy = hierarchy[1:]

    if not hierarchy:
        return None

    # Get Shopware root
    root_resp = client.request_post(
        "search/category",
        {"filter": [{"type": "equals", "field": "parentId", "value": None}], "limit": 1}
    )
    root_cats = root_resp.data or []
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


def get_all_item_categories(item_code: str, include_variant_categories: bool = True) -> list[str]:
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


def sync_all_item_categories(client, item_code: str, skip_sync: bool = False) -> list[dict[str, str]]:
    """
    Sync all categories for an item to Shopware.

    Args:
        client: Shopware API client
        item_code: ERPNext Item code
        skip_sync: If True, only lookup existing categories (much faster)

    Returns:
        List of category ID dicts for Shopware payload
    """
    categories = get_all_item_categories(item_code)
    category_ids = []
    seen_ids = set()

    for item_group_name in categories:
        try:
            if skip_sync:
                # Fast path: only lookup, don't sync
                category_id = get_category_id_fast(client, item_group_name)
            else:
                # Slow path: sync entire hierarchy
                category_id = sync_category_hierarchy(client, item_group_name)

            if category_id and category_id not in seen_ids:
                seen_ids.add(category_id)
                category_ids.append({"id": category_id})
        except Exception as e:
            get_logger().error(f"Error syncing category {item_group_name} for {item_code}: {e}", persist=False)

    return category_ids


def get_category_id_fast(client, category_name: str) -> str | None:
    """
    Fast category ID lookup without syncing.

    Checks cache first, then does a quick Shopware search.
    Does NOT create the category if it doesn't exist.

    Args:
        client: Shopware API client
        category_name: Category name to look up

    Returns:
        Category ID if found, None otherwise
    """
    cache = get_cache()

    # Check cache first
    cached_id = cache.get_category_id(category_name)
    if cached_id:
        return cached_id

    # Quick search in Shopware
    try:
        response = client.request_post(
            "search/category",
            {
                "filter": [{"type": "equals", "field": "name", "value": category_name}],
                "limit": 1,
                "includes": {"category": ["id"]}
            }
        )
        categories = response.data or []
        if categories:
            cat_id = categories[0]["id"]
            cache.set_category_id(category_name, cat_id)
            return cat_id
    except Exception:
        pass

    return None


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

        client.request_post("_action/sync", sync_payload)
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
        product_data = response.data or {}
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
    from ecommerce_integrations.shopware6.export.category_handler import (
        get_all_item_categories,
        sync_category_hierarchy,
    )
    from ecommerce_integrations.shopware6.services.access import require_shopware_admin

    require_shopware_admin()

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
    from ecommerce_integrations.shopware6.services.access import require_shopware_admin

    require_shopware_admin()
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
        product_data = response.data or {}
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
        product_data = response.data or {}
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
    from ecommerce_integrations.shopware6.services.access import require_shopware_admin

    require_shopware_admin()
    try:
        # Find category in Shopware
        response = client.request_post(
            "search/category",
            {"filter": [{"type": "equals", "field": "name", "value": item_group_name}]}
        )
        categories = response.data or []

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
            existing_media = search_resp.data or []
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

        image_path = resolve_category_image_path(item_group_data)
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
        except Exception:
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
        except Exception:
            get_logger().error("Failed to upload media content", persist=False)
            return False

        # Assign to category
        try:
            client.request_patch(f"category/{cat_id}", {"mediaId": fresh_media_id})
            frappe.logger().info(f"Assigned new media {fresh_media_id} to category {item_group_name}")
        except Exception:
            get_logger().error("Failed to assign media to category", persist=False)
            return False

        return True

    except Exception as e:
        get_logger().error(f"Error force resyncing category image for {item_group_name}: {e}", persist=False)
        return False


@temp_shopware_session
def delete_category_from_shopware(client, category_name: str) -> bool:
    """
    Delete a category from Shopware when the corresponding Item Group is deleted in ERPNext.

    Finds the category by name and deletes it via the Shopware API.
    Also invalidates the local cache entry.

    Args:
        client: Shopware API client (injected by decorator)
        category_name: ERPNext Item Group name to delete from Shopware

    Returns:
        True if successful or category didn't exist, False on error
    """
    cache = get_cache()

    try:
        # Skip root categories
        root_to_skip = ROOT_ITEM_GROUPS
        if category_name in root_to_skip:
            return True

        # Search for category by name
        response = client.request_post(
            "search/category",
            {"filter": [{"type": "equals", "field": "name", "value": category_name}]}
        )
        categories = response.data or []

        if not categories:
            get_logger().info(
                f"Category '{category_name}' not found in Shopware, nothing to delete"
            )
            cache.invalidate("category", category_name)
            return True

        cat_id = categories[0]["id"]

        # Delete the category
        client.request_delete(f"category/{cat_id}")

        # Invalidate cache
        cache.invalidate("category", category_name)

        get_logger().info(
            f"Deleted Shopware category '{category_name}' (ID: {cat_id})"
        )

        create_shopware_log(
            status="Success",
            method="delete_category_from_shopware",
            message=f"Deleted category '{category_name}' (ID: {cat_id})",
            make_new=True
        )

        return True

    except Exception as e:
        get_logger().error(
            f"Failed to delete category '{category_name}' from Shopware: {e}",
            persist=False
        )
        create_shopware_log(
            status="Error",
            method="delete_category_from_shopware",
            message=f"Failed to delete category '{category_name}'",
            exception=str(e),
            make_new=True
        )
        return False


@temp_shopware_session
def rename_category_in_shopware(client, old_name: str, new_name: str) -> bool:
    """
    Rename an existing category in Shopware.

    Called when an Item Group is renamed in ERPNext. Finds the category
    by the old name and updates it to the new name.

    Args:
        client: Shopware API client
        old_name: Old Item Group / category name
        new_name: New Item Group / category name

    Returns:
        True if successful, False otherwise
    """
    cache = get_cache()

    try:
        # Skip root categories
        root_to_skip = ROOT_ITEM_GROUPS
        if old_name in root_to_skip or new_name in root_to_skip:
            return True

        # Search for category by old name
        response = client.request_post(
            "search/category",
            {"filter": [{"type": "equals", "field": "name", "value": old_name}]}
        )
        categories = response.data or []

        if not categories:
            # Category doesn't exist in Shopware - nothing to rename
            # The on_update hook will create it with the new name
            frappe.logger("shopware6").info(
                f"Category '{old_name}' not found in Shopware, skipping rename"
            )
            return True

        cat_id = categories[0]["id"]

        # Update the category name
        client.request_patch(f"category/{cat_id}", {"name": new_name})

        # Update cache: remove old name, add new name
        cache.invalidate("category", old_name)
        cache.set_category_id(new_name, cat_id)

        frappe.logger("shopware6").info(
            f"Renamed Shopware category '{old_name}' -> '{new_name}' (ID: {cat_id})"
        )

        create_shopware_log(
            status="Success",
            method="rename_category_in_shopware",
            message=f"Renamed category '{old_name}' -> '{new_name}' (ID: {cat_id})",
            make_new=True
        )

        return True

    except Exception as e:
        get_logger().error(
            f"Failed to rename category '{old_name}' -> '{new_name}': {e}",
            persist=False
        )
        create_shopware_log(
            status="Error",
            method="rename_category_in_shopware",
            message=f"Failed to rename category '{old_name}' -> '{new_name}'",
            exception=str(e),
            make_new=True
        )
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
        root_to_skip = ROOT_ITEM_GROUPS
        if item_group_name in root_to_skip:
            return True

        # Get Item Group data
        item_group_data = get_item_group_data(item_group_name)
        if not item_group_data:
            get_logger().error("Item Group not found", persist=False)
            create_shopware_log(
                status="Error",
                method="sync_item_group_to_shopware",
                message=f"Item Group not found: {item_group_name}",
                make_new=True
            )
            return False

        # Sync the category hierarchy
        category_id = sync_category_hierarchy(client, item_group_name)

        if category_id:
            frappe.logger("shopware6").info(
                f"Synced Item Group '{item_group_name}' to Shopware category {category_id}"
            )
            create_shopware_log(
                status="Success",
                method="sync_item_group_to_shopware",
                message=f"Synced Item Group '{item_group_name}' to Shopware category {category_id}",
                make_new=True
            )

            # Reorder siblings by priority if this category has a priority set
            if item_group_data and item_group_data.get("shopware_priority"):
                try:
                    # Get parent category ID to reorder its children
                    parent_item_group = frappe.db.get_value(
                        "Item Group", item_group_name, "parent_item_group"
                    )
                    if parent_item_group and parent_item_group not in ROOT_ITEM_GROUPS:
                        parent_cat_id = get_category_id_fast(client, parent_item_group)
                        if parent_cat_id:
                            _reorder_children_by_priority(client, parent_cat_id)
                            frappe.logger("shopware6").debug(
                                f"Reordered siblings of '{item_group_name}' by priority"
                            )
                except Exception as e:
                    frappe.logger("shopware6").warning(
                        f"Failed to reorder siblings for '{item_group_name}': {e}"
                    )

            return True
        else:
            get_logger().error(f"Failed to sync Item Group '{item_group_name}' to Shopware", persist=False)
            create_shopware_log(
                status="Error",
                method="sync_item_group_to_shopware",
                message=f"Failed to sync Item Group '{item_group_name}' to Shopware",
                make_new=True
            )
            return False

    except Exception as e:
        get_logger().error(f"Error syncing Item Group '{item_group_name}': {e}", persist=False)
        create_shopware_log(
            status="Error",
            method="sync_item_group_to_shopware",
            message=f"Error syncing Item Group '{item_group_name}'",
            exception=str(e),
            make_new=True
        )
        return False


def bulk_sync_categories(
    client,
    item_groups: list[dict[str, Any]],
    root_parent_id: str | None = None
) -> dict[str, Any]:
    """
    Bulk sync categories to Shopware using Sync API.

    OPTIMIZATION: Instead of N individual API calls (2-3 per category),
    this uses 1 search + 1 bulk upsert = 2 API calls total.

    Args:
        client: Shopware API client
        item_groups: List of Item Group dicts with name, parent_item_group (in lft order!)
        root_parent_id: Shopware root category ID

    Returns:
        Dict with statistics and id_map
    """
    logger = get_logger("bulk_sync_categories")
    cache = get_cache()

    stats = {
        "total": len(item_groups),
        "created": 0,
        "updated": 0,
        "skipped": 0,
        "errors": []
    }

    if not item_groups:
        return {"success": True, "stats": stats, "id_map": {}}

    logger.info(f"[TIMING] Bulk category sync START for {len(item_groups)} categories")

    # Root categories to skip
    root_to_skip = ROOT_ITEM_GROUPS

    try:
        # Step 1: Pre-fetch ALL existing Shopware categories (paginated if needed)
        logger.info("[TIMING] Fetching existing Shopware categories...")
        existing_categories = {}  # name -> {id, parentId}

        # Paginate through all categories (Shopware max limit is 500)
        page = 1
        while True:
            existing_response = client.request_post("search/category", {
                "limit": 500,
                "page": page,
                "includes": {"category": ["id", "name", "parentId"]}
            })
            data = existing_response.data or []
            if not data:
                break
            for cat in data:
                existing_categories[cat.get("name")] = {
                    "id": cat.get("id"),
                    "parentId": cat.get("parentId")
                }
            if len(data) < 500:
                break
            page += 1

        logger.info(f"Found {len(existing_categories)} existing Shopware categories")

        # Step 2: Get Shopware root category if not provided
        if not root_parent_id:
            root_resp = client.request_post(
                "search/category",
                {"filter": [{"type": "equals", "field": "parentId", "value": None}], "limit": 1}
            )
            root_cats = root_resp.data or []
            if root_cats:
                root_parent_id = root_cats[0]["id"]

        # Step 3: Build category payloads
        logger.info("[TIMING] Building category payloads...")
        id_map = {}  # item_group_name -> shopware_id
        create_payloads = []
        update_payloads = []

        for ig in item_groups:
            ig_name = ig.get("name")

            # Skip root categories
            if ig_name in root_to_skip:
                stats["skipped"] += 1
                continue

            # Get Item Group data for SEO, custom fields, etc.
            item_group_data = get_item_group_data(ig_name)

            # Determine parent ID
            parent_name = ig.get("parent_item_group")
            if parent_name in root_to_skip:
                parent_id = root_parent_id
            elif parent_name and parent_name in id_map:
                parent_id = id_map[parent_name]
            elif parent_name and parent_name in existing_categories:
                parent_id = existing_categories[parent_name]["id"]
                id_map[parent_name] = parent_id
            else:
                parent_id = root_parent_id

            # Check if category exists
            if ig_name in existing_categories:
                cat_id = existing_categories[ig_name]["id"]
                id_map[ig_name] = cat_id
                cache.set_category_id(ig_name, cat_id)

                # Build update payload
                payload = _build_category_payload(
                    cat_id, ig_name, parent_id, item_group_data, is_update=True
                )
                update_payloads.append(payload)
                stats["updated"] += 1
            else:
                # Generate new ID
                cat_id = generate_uuid(f"category_{ig_name}")
                id_map[ig_name] = cat_id
                cache.set_category_id(ig_name, cat_id)

                # Build create payload
                payload = _build_category_payload(
                    cat_id, ig_name, parent_id, item_group_data, is_update=False
                )
                create_payloads.append(payload)
                stats["created"] += 1

        logger.info(f"Categories: {stats['created']} to create, {stats['updated']} to update, "
                    f"{stats['skipped']} skipped")

        # Step 4: Execute bulk upsert via Sync API (1 API call)
        all_payloads = create_payloads + update_payloads
        if all_payloads:
            logger.info(f"[TIMING] Executing Sync API for {len(all_payloads)} categories...")

            # Split into chunks to avoid large payloads
            chunk_size = 20
            total_chunks = (len(all_payloads) + chunk_size - 1) // chunk_size
            for i in range(0, len(all_payloads), chunk_size):
                chunk = all_payloads[i:i + chunk_size]
                sync_payload = {
                    "upsert-categories": {
                        "entity": "category",
                        "action": "upsert",
                        "payload": chunk
                    }
                }
                chunk_num = i // chunk_size + 1
                # No retry here — the @temp_shopware_session decorator handles retries.
                # The client has a 60s timeout per request (see connection.py).
                # Use queue-based indexing to avoid SEO URL duplicate key errors.
                client.request_post("_action/sync", sync_payload,
                                    update_header_fields={"indexing-behavior": "use-queue-indexing"})
                logger.info(f"Synced category batch {chunk_num}/{total_chunks}")

        logger.info("[TIMING] Bulk category sync DONE")

        # Step 5: Reorder categories by priority
        logger.info("[TIMING] Reordering categories by priority...")
        try:
            # Get unique parent IDs from the synced categories
            parent_ids_to_reorder = set()
            for ig in item_groups:
                parent_name = ig.get("parent_item_group")
                if parent_name and parent_name not in root_to_skip:
                    if parent_name in id_map:
                        parent_ids_to_reorder.add(id_map[parent_name])
                    elif parent_name in existing_categories:
                        parent_ids_to_reorder.add(existing_categories[parent_name]["id"])

            # Also add root_parent_id to reorder top-level categories
            if root_parent_id:
                parent_ids_to_reorder.add(root_parent_id)

            for pid in parent_ids_to_reorder:
                try:
                    _reorder_children_by_priority(client, pid)
                except (Exception, ShopwareAPIError) as e:
                    logger.warning(f"Failed to reorder children of {pid}: {e}")

            logger.info(f"Reordered children of {len(parent_ids_to_reorder)} parent categories")
        except (Exception, ShopwareAPIError) as e:
            logger.warning(f"Category reordering failed: {e}")

        return {
            "success": True,
            "stats": stats,
            "id_map": id_map
        }

    except (Exception, ShopwareAPIError) as e:
        logger.error(f"Bulk category sync failed: {e}")
        stats["errors"].append(str(e)[:500])
        return {
            "success": False,
            "stats": stats,
            "id_map": {},
            "error": str(e)
        }


def _build_category_payload(
    cat_id: str,
    category_name: str,
    parent_id: str,
    item_group_data: dict[str, Any] | None = None,
    is_update: bool = False
) -> dict[str, Any]:
    """
    Build a category payload for Shopware Sync API.

    Args:
        cat_id: Shopware category UUID
        category_name: Category name
        parent_id: Parent category UUID
        item_group_data: ERPNext Item Group data
        is_update: Whether this is an update (skip some fields)

    Returns:
        Category payload dict
    """
    is_active = True
    if item_group_data and "shopware_active" in item_group_data:
        is_active = item_group_data["shopware_active"]

    payload = {
        "id": cat_id,
        "name": category_name,
        "active": is_active,
        "displayNestedProducts": True,
    }

    # Only set parentId for new categories (can't move existing easily)
    if not is_update and parent_id:
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

        # Custom fields (FAQ, etc.)
        custom_fields = build_category_custom_fields(item_group_data)
        if custom_fields:
            payload["customFields"] = custom_fields

    return payload


def bulk_sync_category_images(
    client,
    item_groups: list[dict[str, Any]],
    id_map: dict[str, str]
) -> dict[str, Any]:
    """
    Bulk sync category images after categories are created.

    Args:
        client: Shopware API client
        item_groups: List of Item Group dicts
        id_map: Mapping of item_group_name -> shopware_category_id

    Returns:
        Dict with statistics
    """
    logger = get_logger("bulk_sync_category_images")
    stats = {"synced": 0, "skipped": 0, "errors": 0}

    for ig in item_groups:
        ig_name = ig.get("name")
        cat_id = id_map.get(ig_name)
        if not cat_id:
            continue

        try:
            item_group_data = get_item_group_data(ig_name)
            if not item_group_data:
                continue

            image_path = resolve_category_image_path(item_group_data)
            if not image_path:
                stats["skipped"] += 1
                continue

            media_id = upload_category_media(client, cat_id, image_path)
            if media_id:
                try:
                    client.request_patch(f"category/{cat_id}", {"mediaId": media_id})
                    stats["synced"] += 1
                except Exception:
                    stats["errors"] += 1
            else:
                stats["skipped"] += 1

        except Exception as e:
            logger.warning(f"Category image sync failed for {ig_name}: {e}")
            stats["errors"] += 1

    return stats


def sync_category_order_by_priority(client, parent_category_id: str | None = None, recursive: bool = True) -> dict[str, Any]:
    """
    Synchronize category order in Shopware based on ERPNext shopware_priority.

    OPTIMIZED VERSION: Loads all categories in one API call, computes order locally,
    then sends a single bulk update.

    Priority logic:
    - Lower priority values appear first (priority 100 before 200)
    - Categories without priority (None/0) are sorted alphabetically at the end
    - Categories with same priority are sorted alphabetically

    Args:
        client: Shopware API client
        parent_category_id: Optional parent category ID. If None, syncs all categories
        recursive: If True, also reorder all subcategories recursively (default: True)

    Returns:
        Dict with sync results
    """
    logger = get_logger("sync_category_order")

    stats = {
        "parents_processed": 0,
        "categories_reordered": 0,
        "errors": []
    }

    try:
        # Step 1: Load ALL categories in one paginated request
        logger.info("[TIMING] Loading all categories from Shopware...")
        all_categories = []
        page = 1
        while True:
            response = client.request_post(
                "search/category",
                {
                    "limit": 500,
                    "page": page,
                    "includes": {"category": ["id", "name", "parentId", "afterCategoryId", "customFields", "childCount"]}
                }
            )
            data = response.data or []
            if not data:
                break
            all_categories.extend(data)
            if len(data) < 500:
                break
            page += 1

        logger.info(f"Loaded {len(all_categories)} categories")

        # Step 2: Build parent -> children mapping
        children_by_parent = {}
        for cat in all_categories:
            parent_id = cat.get("parentId") or cat.get("attributes", {}).get("parentId")
            if parent_id:
                if parent_id not in children_by_parent:
                    children_by_parent[parent_id] = []
                children_by_parent[parent_id].append(cat)

        # Step 3: Determine which parents to process
        if parent_category_id:
            parents_to_process = [parent_category_id]
            if recursive:
                # Add all descendants that have children
                _collect_descendant_parents(parent_category_id, children_by_parent, parents_to_process)
        else:
            # Process all parents that have children
            parents_to_process = list(children_by_parent.keys())

        logger.info(f"Processing {len(parents_to_process)} parent categories")

        # Step 4: Compute new order for each parent
        all_updates = []

        for pid in parents_to_process:
            children = children_by_parent.get(pid, [])
            if len(children) < 2:
                continue

            updates = _compute_order_updates(children)
            if updates:
                all_updates.extend(updates)
                stats["parents_processed"] += 1
                stats["categories_reordered"] += len(updates)

        # Step 5: Send bulk update via Sync API
        if all_updates:
            logger.info(f"[TIMING] Sending {len(all_updates)} order updates...")

            # Split into chunks if too many
            chunk_size = 200
            for i in range(0, len(all_updates), chunk_size):
                chunk = all_updates[i:i + chunk_size]
                sync_payload = {
                    "reorder-categories": {
                        "entity": "category",
                        "action": "upsert",
                        "payload": chunk
                    }
                }
                client.request_post("_action/sync", sync_payload)

        logger.info(f"Category order sync complete: {stats}")

        return {
            "success": len(stats["errors"]) == 0,
            "stats": stats
        }

    except Exception as e:
        logger.error(f"Category order sync failed: {e}")
        return {
            "success": False,
            "error": str(e),
            "stats": stats
        }


def _collect_descendant_parents(parent_id: str, children_by_parent: dict, result: list[str], visited: set | None = None):
    """Recursively collect all descendant parent IDs."""
    if visited is None:
        visited = set()

    if parent_id in visited:
        return
    visited.add(parent_id)

    children = children_by_parent.get(parent_id, [])
    for child in children:
        child_id = child.get("id") or child.get("attributes", {}).get("id")
        child_count = child.get("childCount") or child.get("attributes", {}).get("childCount") or 0
        if child_count > 0 and child_id not in result:
            result.append(child_id)
            _collect_descendant_parents(child_id, children_by_parent, result, visited)


def _compute_order_updates(children: list[dict]) -> list[dict]:
    """
    Compute afterCategoryId updates for a list of sibling categories.

    Returns list of update payloads for categories that need reordering.
    """
    # Sort by priority, then name
    def get_sort_key(cat):
        priority = None
        custom_fields = cat.get("customFields") or cat.get("attributes", {}).get("customFields") or {}
        if isinstance(custom_fields, dict):
            priority = custom_fields.get(SHOPWARE_CATEGORY_PRIORITY)

        name = cat.get("name") or cat.get("attributes", {}).get("name") or ""

        if priority and int(priority) > 0:
            return (0, int(priority), name.lower())
        else:
            return (1, 0, name.lower())

    sorted_children = sorted(children, key=get_sort_key)

    # Build update payloads
    updates = []
    prev_id = None

    for cat in sorted_children:
        cat_id = cat.get("id") or cat.get("attributes", {}).get("id")
        current_after = cat.get("afterCategoryId") or cat.get("attributes", {}).get("afterCategoryId")

        # Only update if the order needs to change
        if current_after != prev_id:
            updates.append({
                "id": cat_id,
                "afterCategoryId": prev_id
            })

        prev_id = cat_id

    return updates


def _reorder_children_by_priority(client, parent_id: str) -> dict[str, Any]:
    """
    Reorder children of a specific parent category by priority.

    Args:
        client: Shopware API client
        parent_id: Parent category ID

    Returns:
        Dict with reorder results including list of children that have their own children
    """
    result = {"reordered": 0, "error": None, "children_with_children": []}

    try:
        # Fetch all children of this parent with their priority and childCount
        response = client.request_post(
            "search/category",
            {
                "filter": [{"type": "equals", "field": "parentId", "value": parent_id}],
                "limit": 500,
                "includes": {"category": ["id", "name", "afterCategoryId", "customFields", "childCount"]}
            }
        )
        children = response.data or []

        if len(children) == 0:
            return result

        # Track children that have their own children (for recursive processing)
        for cat in children:
            cat_id = cat.get("id") or cat.get("attributes", {}).get("id")
            child_count = cat.get("childCount") or cat.get("attributes", {}).get("childCount") or 0
            if child_count > 0:
                result["children_with_children"].append(cat_id)

        if len(children) < 2:
            # Nothing to reorder with 0 or 1 child
            return result

        # Extract priority values and sort
        # Priority: lower number = first, None/0 = last (sorted alphabetically)
        def get_sort_key(cat):
            priority = None
            custom_fields = cat.get("customFields") or cat.get("attributes", {}).get("customFields") or {}
            if isinstance(custom_fields, dict):
                priority = custom_fields.get(SHOPWARE_CATEGORY_PRIORITY)

            name = cat.get("name") or cat.get("attributes", {}).get("name") or ""

            # Categories with priority sort by priority first, then name
            # Categories without priority (None or 0) sort last, alphabetically
            if priority and int(priority) > 0:
                return (0, int(priority), name.lower())
            else:
                return (1, 0, name.lower())

        sorted_children = sorted(children, key=get_sort_key)

        # Build update payloads for afterCategoryId
        updates = []
        prev_id = None

        for cat in sorted_children:
            cat_id = cat.get("id") or cat.get("attributes", {}).get("id")
            current_after = cat.get("afterCategoryId") or cat.get("attributes", {}).get("afterCategoryId")

            # Only update if the order needs to change
            if current_after != prev_id:
                updates.append({
                    "id": cat_id,
                    "afterCategoryId": prev_id  # None for first, prev_id for others
                })

            prev_id = cat_id

        if not updates:
            return result

        # Use Sync API for bulk update
        sync_payload = {
            "reorder-categories": {
                "entity": "category",
                "action": "upsert",
                "payload": updates
            }
        }

        client.request_post("_action/sync", sync_payload)
        result["reordered"] = len(updates)

        return result

    except Exception as e:
        result["error"] = str(e)[:200]
        return result


@frappe.whitelist()
@temp_shopware_session
def sync_all_category_orders(client) -> dict[str, Any]:
    """
    Sync category order for ALL parent categories based on priority.

    This is the main entry point for reordering all categories in Shopware
    based on their ERPNext shopware_priority values.

    Usage:
        bench execute ecommerce_integrations.shopware6.export.category_handler.sync_all_category_orders

    Returns:
        Dict with sync results
    """
    from ecommerce_integrations.shopware6.services.access import require_shopware_admin

    require_shopware_admin()
    return sync_category_order_by_priority(client, parent_category_id=None)


@frappe.whitelist()
@temp_shopware_session
def migrate_add_priority_custom_field(client) -> dict[str, Any]:
    """
    Migration: Add the priority custom field to existing category custom field set.

    Run this once after deploying the priority feature if the custom field set
    already exists in Shopware.

    Usage:
        bench execute ecommerce_integrations.shopware6.export.category_handler.migrate_add_priority_custom_field

    Returns:
        Dict with migration result
    """
    from ecommerce_integrations.shopware6.services.access import require_shopware_admin

    require_shopware_admin()
    logger = get_logger("migrate_priority_field")

    try:
        # Find existing custom field set
        response = client.request_post(
            "search/custom-field-set",
            {
                "filter": [{"type": "equals", "field": "name", "value": SHOPWARE_CATEGORY_CUSTOM_FIELD_SET_NAME}],
                "associations": {"customFields": {}}
            }
        )
        sets = response.data or []

        if not sets:
            return {
                "success": False,
                "message": f"Custom field set '{SHOPWARE_CATEGORY_CUSTOM_FIELD_SET_NAME}' not found. It will be created on next category sync."
            }

        set_data = sets[0]
        set_id = set_data["id"]
        existing_fields = set_data.get("customFields", [])

        # Check if priority field already exists
        priority_exists = any(f.get("name") == SHOPWARE_CATEGORY_PRIORITY for f in existing_fields)

        if priority_exists:
            return {
                "success": True,
                "message": f"Priority field '{SHOPWARE_CATEGORY_PRIORITY}' already exists. No migration needed."
            }

        # Determine position (after existing fields)
        max_position = max((f.get("config", {}).get("customFieldPosition", 0) for f in existing_fields), default=0)
        new_position = max_position + 1

        # Create the priority custom field
        field_id = generate_uuid(f"custom_field_{SHOPWARE_CATEGORY_PRIORITY}")
        field_payload = {
            "id": field_id,
            "name": SHOPWARE_CATEGORY_PRIORITY,
            "type": "int",
            "customFieldSetId": set_id,
            "config": {
                "label": {"de-DE": "Sortier-Priorität", "en-GB": "Sort Priority"},
                "customFieldPosition": new_position,
                "helpText": {"de-DE": "Niedrigere Zahlen erscheinen zuerst", "en-GB": "Lower numbers appear first"},
            },
        }

        client.request_post("custom-field", field_payload)

        logger.info("Successfully added priority custom field to category custom field set")

        return {
            "success": True,
            "message": f"Successfully added '{SHOPWARE_CATEGORY_PRIORITY}' field to '{SHOPWARE_CATEGORY_CUSTOM_FIELD_SET_NAME}'",
            "field_id": field_id,
            "position": new_position
        }

    except Exception as e:
        logger.error(f"Migration failed: {e}")
        return {
            "success": False,
            "error": str(e)
        }
