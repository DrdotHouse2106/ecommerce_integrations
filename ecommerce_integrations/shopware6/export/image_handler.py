"""
Shopware 6 Image Handler

Handles image synchronization from ERPNext Items to Shopware products.
Implements delta-sync using MD5 hashes to avoid redundant uploads.
"""

import hashlib
import mimetypes
import os
import re
from typing import List, Optional, Tuple

import frappe
import requests
from frappe.utils import get_files_path

from ecommerce_integrations.shopware6.base.cache_manager import get_cache
from ecommerce_integrations.shopware6.export.utils import generate_uuid, sanitize_filename
from ecommerce_integrations.shopware6.utils import get_logger


def get_product_media_folder_id(client) -> Optional[str]:
    """
    Get the Product Media folder ID in Shopware.

    Args:
        client: Shopware API client

    Returns:
        Folder ID if found, None otherwise
    """
    cache = get_cache()
    cached_id = cache.get("media_folder", "product")
    if cached_id:
        return cached_id

    try:
        response = client.request_post(
            "search/media-folder",
            {"filter": [{"type": "equals", "field": "name", "value": "Product Media"}]}
        )
        folders = response.get("data", [])

        if folders:
            folder_id = folders[0]["id"]
            cache.set("media_folder", "product", folder_id)
            return folder_id

        # Try default folder
        response = client.request_post(
            "search/media-default-folder",
            {"filter": [{"type": "equals", "field": "entity", "value": "product"}]}
        )
        default_folders = response.get("data", [])
        if default_folders:
            folder_id = default_folders[0].get("folderId")
            if folder_id:
                cache.set("media_folder", "product", folder_id)
                return folder_id

        return None

    except Exception as e:
        get_logger().error("Failed to get Product Media folder", persist=False)
        return None


def _get_file_doc_from_url(file_url: str):
    """
    Get File document from a dfp_external_storage URL.

    URLs have format: /file/{file_id}/{filename}

    Args:
        file_url: ERPNext file URL

    Returns:
        File document or None
    """
    match = re.search(r'^/file/([^/]+)/(.+)$', file_url)
    if match:
        file_id = match.group(1)
        try:
            return frappe.get_doc("File", file_id)
        except Exception:
            pass
    return None


def get_file_content_and_type(file_url: str) -> Tuple[Optional[bytes], Optional[str], Optional[str]]:
    """
    Get file content and mime type from ERPNext file URL.

    Supports:
    - HTTP/HTTPS URLs
    - Local files (/files/, /private/files/)
    - dfp_external_storage URLs (/file/{id}/{filename})

    Args:
        file_url: ERPNext file URL

    Returns:
        Tuple of (file_content, mime_type, extension) or (None, None, None)
    """
    try:
        if not file_url:
            return None, None, None

        if file_url.startswith(('http://', 'https://')):
            response = requests.get(file_url, timeout=30)
            if response.status_code == 200:
                content_type = response.headers.get('Content-Type', 'image/jpeg')
                ext = mimetypes.guess_extension(content_type) or '.jpg'
                return response.content, content_type, ext.lstrip('.')
            return None, None, None

        # dfp_external_storage URL format: /file/{file_id}/{filename}
        if file_url.startswith('/file/'):
            file_doc = _get_file_doc_from_url(file_url)
            if file_doc:
                try:
                    content = file_doc.get_content()
                    if content:
                        mime_type, _ = mimetypes.guess_type(file_doc.file_name)
                        mime_type = mime_type or 'image/jpeg'
                        ext = os.path.splitext(file_doc.file_name)[1].lstrip('.') or 'jpg'
                        return content, mime_type, ext
                except Exception as e:
                    get_logger().error(f"Failed to get content from external storage for {file_url}: {str(e)}", persist=False)
            return None, None, None

        # Local file
        if file_url.startswith('/private/files/'):
            file_path = os.path.join(frappe.get_site_path(), file_url.lstrip('/'))
        elif file_url.startswith('/files/'):
            file_path = os.path.join(frappe.get_site_path(), 'public', file_url.lstrip('/'))
        else:
            file_path = os.path.join(get_files_path(), file_url.lstrip('/'))

        if os.path.exists(file_path):
            with open(file_path, 'rb') as f:
                content = f.read()
            mime_type, _ = mimetypes.guess_type(file_path)
            mime_type = mime_type or 'image/jpeg'
            ext = os.path.splitext(file_path)[1].lstrip('.') or 'jpg'
            return content, mime_type, ext

        return None, None, None

    except Exception as e:
        get_logger().error(f"Failed to get file content for {file_url}: {str(e)}", persist=False)
        return None, None, None


def get_file_hash(file_url: str) -> Optional[str]:
    """
    Calculate MD5 hash of a file for delta-sync.

    Supports:
    - Local files (/files/, /private/files/)
    - dfp_external_storage URLs (/file/{id}/{filename})

    Args:
        file_url: ERPNext file URL

    Returns:
        MD5 hash string or None
    """
    try:
        if not file_url or file_url.startswith(('http://', 'https://')):
            return None

        # dfp_external_storage URL format: /file/{file_id}/{filename}
        if file_url.startswith('/file/'):
            file_doc = _get_file_doc_from_url(file_url)
            if file_doc:
                # Use content_hash from File doc if available (more efficient)
                if file_doc.content_hash:
                    return file_doc.content_hash
                # Otherwise fetch content and calculate hash
                try:
                    content = file_doc.get_content()
                    if content:
                        return hashlib.md5(content).hexdigest()
                except Exception:
                    pass
            return None

        if file_url.startswith('/private/files/'):
            file_path = os.path.join(frappe.get_site_path(), file_url.lstrip('/'))
        elif file_url.startswith('/files/'):
            file_path = os.path.join(frappe.get_site_path(), 'public', file_url.lstrip('/'))
        else:
            file_path = os.path.join(get_files_path(), file_url.lstrip('/'))

        if os.path.exists(file_path):
            with open(file_path, 'rb') as f:
                return hashlib.md5(f.read()).hexdigest()

        return None
    except Exception:
        return None


def upload_media_to_shopware(client, file_url: str, item_code: str, position: int = 0) -> Optional[str]:
    """
    Upload a media file to Shopware and return the media ID.

    Args:
        client: Shopware API client
        file_url: ERPNext file URL
        item_code: Item code for naming
        position: Image position

    Returns:
        Media ID if successful, None otherwise
    """
    try:
        file_content, mime_type, extension = get_file_content_and_type(file_url)
        if not file_content:
            get_logger().warning(f"Could not read file {file_url} for item {item_code}", persist=False)
            return None

        folder_id = get_product_media_folder_id(client)
        media_id = generate_uuid(f"media_{item_code}_{position}")

        # Check if media exists
        media_exists = False
        try:
            response = client.request_get(f"media/{media_id}")
            if response.get("data"):
                media_exists = True
        except BaseException:
            pass

        if not media_exists:
            media_payload = {"id": media_id}
            if folder_id:
                media_payload["mediaFolderId"] = folder_id

            try:
                client.request_post("media", media_payload)
            except BaseException as e:
                error_str = str(e).lower()
                if "already exists" not in error_str and "updatecommand" not in error_str:
                    get_logger().error(f"Failed to create media for {item_code}: {str(e)}", persist=False)
                    return None

        # Check if already has content
        try:
            media_check = client.request_get(f"media/{media_id}")
            if media_check.get("data", {}).get("fileName"):
                return media_id
        except BaseException as e:
            get_logger().warning(f"Media check failed for {item_code}: {str(e)}", persist=False)
            # Continue with upload attempt

        # Upload file
        filename = sanitize_filename(f"{item_code}_{position}")
        try:
            client.request_post(
                f"_action/media/{media_id}/upload",
                payload=file_content,
                content_type="octet-stream",
                additional_query_params={"extension": extension, "fileName": filename}
            )
        except BaseException as e:
            error_str = str(e).lower()
            if "already exists" not in error_str and "duplicate" not in error_str:
                get_logger().error(f"Failed to upload file for {item_code}: {str(e)}", persist=False)
                return None

        return media_id

    except BaseException as e:
        get_logger().error(f"Failed to upload media for {item_code}: {str(e)}", persist=False)
        return None


def get_item_images(item) -> List[str]:
    """
    Get all unique image URLs for an item.

    Returns list of file URLs, with main image first.
    Filters duplicates based on hash and filename.

    Args:
        item: ERPNext Item document

    Returns:
        List of image URLs
    """
    images = []
    seen_hashes = set()
    seen_filenames = set()

    def get_base_filename(url: str) -> str:
        if not url:
            return ""
        filename = url.split('/')[-1]
        if '.' in filename:
            filename = filename.rsplit('.', 1)[0]
        return filename

    # Main image first
    if item.image:
        main_hash = get_file_hash(item.image)
        images.append(item.image)
        if main_hash:
            seen_hashes.add(main_hash)
        main_basename = get_base_filename(item.image)
        if main_basename:
            seen_filenames.add(main_basename)

    # Get attachments sorted by filename for consistent ordering
    # This ensures images like item_01.jpg, item_02.jpg are in correct order
    attachments = frappe.get_all(
        "File",
        filters={
            "attached_to_doctype": "Item",
            "attached_to_name": item.name,
            "is_folder": 0,
        },
        fields=["file_url", "file_name"],
        order_by="file_name asc"
    )

    image_extensions = ('.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp')

    for attachment in attachments:
        file_url = attachment.file_url
        if not file_url or not file_url.lower().endswith(image_extensions):
            continue

        if file_url in images:
            continue

        # Check for truncated duplicates
        attachment_basename = get_base_filename(file_url)
        is_duplicate = False
        for seen_name in seen_filenames:
            if seen_name.startswith(attachment_basename) or attachment_basename.startswith(seen_name):
                is_duplicate = True
                break

        if is_duplicate:
            continue

        # Check content hash
        file_hash = get_file_hash(file_url)
        if file_hash:
            if file_hash in seen_hashes:
                continue
            seen_hashes.add(file_hash)

        images.append(file_url)
        if attachment_basename:
            seen_filenames.add(attachment_basename)

    return images


def clear_product_images_in_shopware(client, shopware_product_id: str, item_code: str = None) -> int:
    """
    Delete all images from a Shopware product.

    Use this when ERPNext has no images but Shopware still has linked media.

    Args:
        client: Shopware API client
        shopware_product_id: Shopware product UUID
        item_code: Item code for cache clearing (optional)

    Returns:
        Number of images deleted
    """
    deleted_count = 0
    cache = get_cache()

    try:
        # Get all product-media relationships
        search_result = client.request_post("search/product-media", {
            "filter": [{"type": "equals", "field": "productId", "value": shopware_product_id}],
            "includes": {"product_media": ["id", "mediaId"]}
        })
        existing = search_result.get("data", []) or []

        if not existing:
            return 0

        media_ids_to_delete = []

        # Delete product-media relationships first
        for pm in existing:
            pm_id = pm.get("id")
            media_id = pm.get("mediaId")
            if pm_id:
                try:
                    client.request_delete(f"product-media/{pm_id}")
                    deleted_count += 1
                    if media_id:
                        media_ids_to_delete.append(media_id)
                except BaseException as e:
                    frappe.logger().warning(f"Could not delete product-media {pm_id}: {str(e)[:50]}")

        # Then delete the media objects themselves
        for media_id in media_ids_to_delete:
            try:
                client.request_delete(f"media/{media_id}")
            except BaseException:
                pass  # Media might be used elsewhere

        # Clear cache
        if item_code:
            cache.clear_image_hashes(item_code)

        if deleted_count > 0:
            frappe.logger().info(f"Cleared {deleted_count} images from Shopware product {shopware_product_id}")

        return deleted_count

    except BaseException as e:
        frappe.logger().warning(f"Error clearing images for {shopware_product_id}: {str(e)[:100]}")
        return deleted_count


def sync_product_images_to_shopware(client, item, shopware_product_id: str) -> bool:
    """
    Sync all images from ERPNext Item to Shopware product.

    Uses delta-sync: only uploads images if content has changed.
    If ERPNext has no images, all Shopware images are deleted.

    Args:
        client: Shopware API client
        item: ERPNext Item document
        shopware_product_id: Shopware product UUID

    Returns:
        True if successful
    """
    cache = get_cache()

    try:
        images = get_item_images(item)

        # If no images in ERPNext, clear all Shopware images and return
        if not images:
            clear_product_images_in_shopware(client, shopware_product_id, item.item_code)
            return True

        # Remove existing product-media relationships before uploading new ones
        try:
            search_result = client.request_post("search/product-media", {
                "filter": [{"type": "equals", "field": "productId", "value": shopware_product_id}]
            })
            existing = search_result.get("data", []) or []

            if existing:
                media_ids_to_delete = []
                for pm in existing:
                    pm_id = pm.get("id")
                    media_id = pm.get("mediaId") or (pm.get("media") or {}).get("id")
                    if pm_id:
                        try:
                            client.request_delete(f"product-media/{pm_id}")
                            if media_id:
                                media_ids_to_delete.append(media_id)
                        except BaseException:
                            pass

                for media_id in media_ids_to_delete:
                    try:
                        client.request_delete(f"media/{media_id}")
                    except BaseException:
                        pass

                cache.clear_image_hashes(item.item_code)
        except BaseException as e:
            frappe.logger().warning(f"Could not clean media for {item.item_code}: {e}")

        product_media_list = []
        cover_id = None
        images_uploaded = 0
        images_skipped = 0

        for position, image_url in enumerate(images):
            current_hash = get_file_hash(image_url)
            cached_hash = cache.get_image_hash(item.item_code, position)

            if current_hash and cached_hash and current_hash == cached_hash:
                media_id = generate_uuid(f"media_{item.item_code}_{position}")
                images_skipped += 1
            else:
                media_id = upload_media_to_shopware(client, image_url, item.item_code, position)
                if media_id and current_hash:
                    cache.set_image_hash(item.item_code, position, current_hash)
                    images_uploaded += 1

            if media_id:
                product_media_id = generate_uuid(f"product_media_{item.item_code}_{position}")
                product_media_list.append({
                    "id": product_media_id,
                    "mediaId": media_id,
                    "position": position,
                })
                if position == 0:
                    cover_id = product_media_id

        if product_media_list:
            update_payload = {"media": product_media_list}
            if cover_id:
                update_payload["coverId"] = cover_id
            client.request_patch(f"product/{shopware_product_id}", update_payload)

            if images_uploaded > 0 or images_skipped > 0:
                frappe.logger().info(
                    f"Image sync for {item.item_code}: {images_uploaded} uploaded, {images_skipped} skipped"
                )

        return True

    except Exception as e:
        get_logger().error(f"Failed to sync images for {item.name}: {str(e)}", persist=False)
        return False
