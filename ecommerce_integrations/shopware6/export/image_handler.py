"""
Shopware 6 Image Handler

Handles image synchronization from ERPNext Items to Shopware products.
Implements delta-sync using MD5 hashes to avoid redundant uploads.
"""

import hashlib
import mimetypes
import os
import re
from typing import List, NamedTuple, Optional, Tuple

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

    except BaseException as e:
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


def _get_public_image_url(file_url: str) -> Optional[Tuple[str, str]]:
    """
    Convert ERPNext file URL to a publicly accessible URL for Shopware to fetch.

    Args:
        file_url: ERPNext file URL (/files/..., /file/..., etc.)

    Returns:
        Tuple of (public_url, extension) if accessible, None otherwise
    """
    if not file_url:
        return None

    # Already a full URL
    if file_url.startswith(('http://', 'https://')):
        ext = os.path.splitext(file_url)[1].lstrip('.') or 'jpg'
        return file_url, ext

    # Private files - not publicly accessible
    if file_url.startswith('/private/'):
        return None

    # Get site URL
    site_url = frappe.utils.get_url().rstrip('/')

    # Public files: /files/... or /file/... (dfp_external_storage)
    if file_url.startswith('/files/') or file_url.startswith('/file/'):
        public_url = f"{site_url}{file_url}"
        ext = os.path.splitext(file_url)[1].lstrip('.') or 'jpg'
        return public_url, ext

    return None


def _upload_media_via_url(client, media_id: str, public_url: str, extension: str, filename: str) -> bool:
    """
    Upload media to Shopware using URL (Shopware fetches the image).

    OPTIMIZATION: Much faster than binary upload - no need to download/upload binary data.

    Args:
        client: Shopware API client
        media_id: Shopware media UUID
        public_url: Publicly accessible image URL
        extension: File extension (jpg, png, etc.)
        filename: Desired filename in Shopware

    Returns:
        True if successful, False otherwise
    """
    try:
        # Shopware endpoint accepts JSON with URL
        # Note: lib_shopware6_api_base expects "json" not "application/json"
        # (it adds the "application/" prefix automatically)
        client.request_post(
            f"_action/media/{media_id}/upload",
            payload={"url": public_url},
            content_type="json",
            additional_query_params={"extension": extension, "fileName": filename}
        )
        return True
    except BaseException as e:
        error_str = str(e).lower()
        # Ignore "already exists" errors
        if "already exists" in error_str or "duplicate" in error_str:
            return True
        get_logger().debug(f"URL-based upload failed for {public_url}: {str(e)[:100]}", persist=False)
        return False


def upload_media_to_shopware(client, file_url: str, item_code: str, position: int = 0) -> Optional[str]:
    """
    Upload a media file to Shopware and return the media ID.

    OPTIMIZATION: Uses URL-based upload when possible (Shopware fetches the image).
    Falls back to binary upload for private files or if URL-based fails.

    Args:
        client: Shopware API client
        file_url: ERPNext file URL
        item_code: Item code for naming
        position: Image position

    Returns:
        Media ID if successful, None otherwise
    """
    file_content = None  # Initialize for cleanup in finally block
    
    try:
        folder_id = get_product_media_folder_id(client)
        media_id = generate_uuid(f"media_{item_code}_{position}")
        filename = sanitize_filename(f"{item_code}_{position}")

        # Check if media already exists with content
        try:
            media_check = client.request_get(f"media/{media_id}")
            if media_check.get("data", {}).get("fileName"):
                return media_id  # Already uploaded
        except BaseException:
            pass  # Media doesn't exist yet

        # Create media entity first
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

        # Try URL-based upload first (much faster - Shopware fetches directly)
        public_url_info = _get_public_image_url(file_url)
        if public_url_info:
            public_url, extension = public_url_info
            if _upload_media_via_url(client, media_id, public_url, extension, filename):
                return media_id
            # URL-based failed, fall back to binary

        # Binary upload fallback (for private files or if URL-based failed)
        file_content, mime_type, extension = get_file_content_and_type(file_url)
        if not file_content:
            get_logger().warning(f"Could not read file {file_url} for item {item_code}", persist=False)
            return None

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
    finally:
        # Explicit memory cleanup for binary content
        if file_content is not None:
            del file_content


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


def _bulk_delete_product_media(client, product_media_ids: List[str], media_ids: List[str]) -> Tuple[int, int]:
    """
    Bulk delete product-media relationships and media objects using Sync API.

    OPTIMIZATION: Instead of N individual DELETE requests, uses 1-2 Sync API calls.

    Args:
        client: Shopware API client
        product_media_ids: List of product-media UUIDs to delete
        media_ids: List of media UUIDs to delete

    Returns:
        Tuple of (product_media_deleted, media_deleted)
    """
    pm_deleted = 0
    media_deleted = 0

    if not product_media_ids and not media_ids:
        return pm_deleted, media_deleted

    try:
        sync_payload = {}

        # Bulk delete product-media relationships
        if product_media_ids:
            sync_payload["delete-product-media"] = {
                "entity": "product_media",
                "action": "delete",
                "payload": [{"id": pm_id} for pm_id in product_media_ids]
            }

        # Bulk delete media objects
        if media_ids:
            sync_payload["delete-media"] = {
                "entity": "media",
                "action": "delete",
                "payload": [{"id": m_id} for m_id in media_ids]
            }

        if sync_payload:
            client.request_post("_action/sync", sync_payload)
            pm_deleted = len(product_media_ids)
            media_deleted = len(media_ids)

    except BaseException as e:
        # Log but don't fail - media might be used elsewhere (BaseException for ShopwareAPIError)
        frappe.logger().warning(f"Bulk media delete partially failed: {str(e)[:100]}")
        # Try to return what we attempted
        pm_deleted = len(product_media_ids) if product_media_ids else 0

    return pm_deleted, media_deleted


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

        # Collect IDs for bulk delete
        product_media_ids = []
        media_ids_to_delete = []

        for pm in existing:
            pm_id = pm.get("id")
            media_id = pm.get("mediaId")
            if pm_id:
                product_media_ids.append(pm_id)
            if media_id:
                media_ids_to_delete.append(media_id)

        # Bulk delete using Sync API (much faster than individual deletes)
        deleted_count, _ = _bulk_delete_product_media(client, product_media_ids, media_ids_to_delete)

        # Clear cache
        if item_code:
            cache.clear_image_hashes(item_code)

        if deleted_count > 0:
            frappe.logger().info(f"Cleared {deleted_count} images from Shopware product {shopware_product_id}")

        return deleted_count

    except BaseException as e:
        frappe.logger().warning(f"Error clearing images for {shopware_product_id}: {str(e)[:100]}")
        return deleted_count


class MediaHealthResult(NamedTuple):
    """Result of media health check with data for reuse."""
    is_healthy: bool
    existing_media: list  # Reusable for delete operation


# Cache position key constant for maintainability
IMAGE_COUNT_CACHE_KEY = -1


def _verify_shopware_media_health(client, item_code: str, shopware_product_id: str, expected_count: int) -> MediaHealthResult:
    """
    Verify that all product images actually exist and have content in Shopware.

    This health check ensures 100% reliability by checking:
    1. Product has the correct number of product-media associations
    2. Each association is a proper dict (not JSON string or malformed)
    3. Each associated media entity exists as proper dict
    4. Each media has fileName (= content was uploaded successfully)
    5. All expected positions (0 to n-1) are present
    6. Media IDs match expected pattern for this item

    OPTIMIZATION: Returns existing media data for reuse in delete operation,
    avoiding a second API call.

    Args:
        client: Shopware API client
        item_code: ERPNext item code (for generating expected media IDs)
        shopware_product_id: Shopware product UUID
        expected_count: Expected number of images

    Returns:
        MediaHealthResult with is_healthy flag and existing_media for reuse
    """
    try:
        # Get all product-media associations with their media entities
        search_result = client.request_post("search/product-media", {
            "filter": [{"type": "equals", "field": "productId", "value": shopware_product_id}],
            "associations": {"media": {}},  # Include media entity details
            "limit": expected_count + 5  # Small buffer
        })
        existing = search_result.get("data", []) or []

        # Check 1: Correct count of associations
        if len(existing) != expected_count:
            return MediaHealthResult(is_healthy=False, existing_media=existing)

        # Track which positions we found
        found_positions = set()
        expected_media_ids = {
            generate_uuid(f"media_{item_code}_{pos}") for pos in range(expected_count)
        }
        found_media_ids = set()

        for pm in existing:
            # Check 2: product-media is a proper dict (not JSON string)
            if not isinstance(pm, dict):
                return MediaHealthResult(is_healthy=False, existing_media=existing)

            # Check 3: Has valid position
            position = pm.get("position")
            if position is None or not isinstance(position, int):
                return MediaHealthResult(is_healthy=False, existing_media=existing)
            found_positions.add(position)

            # Check 4: media is a proper dict (not JSON string or None)
            media = pm.get("media")
            if not isinstance(media, dict):
                return MediaHealthResult(is_healthy=False, existing_media=existing)

            # Check 5: media has fileName (= content was uploaded)
            if not media.get("fileName"):
                return MediaHealthResult(is_healthy=False, existing_media=existing)

            # Check 6: media has valid ID
            media_id = pm.get("mediaId") or media.get("id")
            if not media_id or not isinstance(media_id, str):
                return MediaHealthResult(is_healthy=False, existing_media=existing)
            found_media_ids.add(media_id)

        # Check 7: All expected positions present (0, 1, 2, ... n-1)
        expected_positions = set(range(expected_count))
        if found_positions != expected_positions:
            return MediaHealthResult(is_healthy=False, existing_media=existing)

        # Check 8: Media IDs match expected pattern (deterministic UUIDs)
        if found_media_ids != expected_media_ids:
            return MediaHealthResult(is_healthy=False, existing_media=existing)

        return MediaHealthResult(is_healthy=True, existing_media=existing)

    except BaseException:
        # If we can't verify, assume unhealthy to be safe
        return MediaHealthResult(is_healthy=False, existing_media=[])


def sync_product_images_to_shopware(client, item, shopware_product_id: str, cache=None) -> bool:
    """
    Sync all images from ERPNext Item to Shopware product.

    OPTIMIZED delta-sync with 100% reliable health check:
    1. First checks if ALL images have matching hashes (nothing changed locally)
    2. If hashes match, verifies images actually exist in Shopware (health check)
    3. Only if BOTH pass, skips the sync - otherwise does full re-sync

    Args:
        client: Shopware API client
        item: ERPNext Item document
        shopware_product_id: Shopware product UUID
        cache: Optional cache instance (for thread-safety in parallel sync).
               If not provided, uses global get_cache().

    Returns:
        True if successful
    """
    if cache is None:
        cache = get_cache()

    try:
        images = get_item_images(item)

        # If no images in ERPNext, clear all Shopware images and return
        if not images:
            clear_product_images_in_shopware(client, shopware_product_id, item.item_code)
            return True

        # OPTIMIZATION: Check if ALL images are unchanged BEFORE doing expensive API calls
        all_hashes_match = True
        image_hashes = []  # Store hashes to avoid recalculating

        for position, image_url in enumerate(images):
            current_hash = get_file_hash(image_url)
            cached_hash = cache.get_image_hash(item.item_code, position)
            image_hashes.append(current_hash)

            if not (current_hash and cached_hash and current_hash == cached_hash):
                all_hashes_match = False
                # Don't break - we need all hashes for later

        # Also check if image count changed (cached count stored as special key)
        cached_count = cache.get_image_hash(item.item_code, IMAGE_COUNT_CACHE_KEY)
        if cached_count != str(len(images)):
            all_hashes_match = False

        # Variable to store existing media from health check (for reuse)
        existing_media_from_health_check = None

        if all_hashes_match:
            # Hashes match - but verify images actually exist in Shopware (100% reliable)
            health_result = _verify_shopware_media_health(client, item.item_code, shopware_product_id, len(images))
            if health_result.is_healthy:
                # All good - skip sync entirely
                return True
            # Health check failed - store existing data for reuse, clear cache
            existing_media_from_health_check = health_result.existing_media
            cache.clear_image_hashes(item.item_code)

        # Something changed - need to do full sync
        # Remove existing product-media relationships before uploading new ones
        try:
            # OPTIMIZATION: Reuse data from health check if available (avoids 2nd API call)
            if existing_media_from_health_check is not None:
                existing = existing_media_from_health_check
            else:
                search_result = client.request_post("search/product-media", {
                    "filter": [{"type": "equals", "field": "productId", "value": shopware_product_id}]
                })
                existing = search_result.get("data", []) or []

            if existing:
                product_media_ids = []
                media_ids_to_delete = []

                for pm in existing:
                    pm_id = pm.get("id")
                    media_id = pm.get("mediaId") or (pm.get("media") or {}).get("id")
                    if pm_id:
                        product_media_ids.append(pm_id)
                    if media_id:
                        media_ids_to_delete.append(media_id)

                _bulk_delete_product_media(client, product_media_ids, media_ids_to_delete)
                cache.clear_image_hashes(item.item_code)
        except BaseException as e:
            frappe.logger().warning(f"Could not clean media for {item.item_code}: {e}")

        product_media_list = []
        cover_id = None
        images_uploaded = 0

        for position, image_url in enumerate(images):
            current_hash = image_hashes[position]  # Use pre-calculated hash

            # Upload the image
            media_id = upload_media_to_shopware(client, image_url, item.item_code, position)
            if media_id:
                if current_hash:
                    cache.set_image_hash(item.item_code, position, current_hash)
                images_uploaded += 1

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

            # Store image count for next delta check
            cache.set_image_hash(item.item_code, IMAGE_COUNT_CACHE_KEY, str(len(images)))

            frappe.logger().info(
                f"Image sync for {item.item_code}: {images_uploaded} uploaded"
            )

        return True

    except BaseException as e:
        # NOTE: Must catch BaseException because ShopwareAPIError inherits from BaseException, not Exception!
        get_logger().error(f"Failed to sync images for {item.name}: {str(e)}", persist=False)
        return False


def batch_sync_images(item_codes: list) -> dict:
    """
    Batch sync images for multiple items to Shopware.

    Args:
        item_codes: List of ERPNext item codes to sync images for

    Returns:
        Dict with sync statistics
    """
    from ecommerce_integrations.shopware6.connection import get_shopware_client
    from ecommerce_integrations.shopware6.export.utils import get_shopware_document_id
    from ecommerce_integrations.shopware6.utils import get_logger

    logger = get_logger("ImageSync")
    result = {"success": 0, "failed": 0, "skipped": 0, "errors": []}

    client = get_shopware_client()
    if not client:
        result["errors"].append("Could not get Shopware client")
        return result

    total = len(item_codes)
    logger.info(f"Starting batch image sync for {total} items", persist=True)

    for i, item_code in enumerate(item_codes, 1):
        try:
            shopware_id = get_shopware_document_id("Item", item_code)
            if not shopware_id:
                result["skipped"] += 1
                continue

            item = frappe.get_doc("Item", item_code)
            success = sync_product_images_to_shopware(client, item, shopware_id)

            if success:
                result["success"] += 1
            else:
                result["failed"] += 1

        except Exception as e:
            result["failed"] += 1
            if len(result["errors"]) < 50:
                result["errors"].append(f"{item_code}: {str(e)[:80]}")

        # Progress log every 100 items - persist to Ecommerce Integration Log for UI visibility
        if i % 100 == 0:
            logger.info(
                f"Image Sync Progress: {i}/{total} items - "
                f"{result['success']} success, {result['failed']} failed, {result['skipped']} skipped",
                persist=True
            )
            frappe.db.commit()  # Ensure logs are visible immediately

    logger.info(
        f"Image Sync Complete: {result['success']} success, {result['failed']} failed, {result['skipped']} skipped",
        persist=True
    )
    return result
