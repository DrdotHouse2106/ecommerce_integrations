"""Test binary upload fix with a single category."""
import frappe
from ecommerce_integrations.shopware6.connection import temp_shopware_session
from ecommerce_integrations.shopware6.export.utils import generate_uuid
from ecommerce_integrations.shopware6.export.category_handler import (
    get_image_content_and_filename,
    get_or_create_media_folder,
)
import time
import hashlib
from ecommerce_integrations.shopware6.utils import require_shopware_admin


@frappe.whitelist()
@temp_shopware_session
def test_upload(client, category_name="Werkbänke"):
    """Test uploading a category image with the binary fix."""
    require_shopware_admin()
    # Get item group data
    ig = frappe.get_doc("Item Group", category_name)
    img_path = getattr(ig, "category_image", None) or ig.image
    print(f"\n=== Testing upload for: {category_name} ===")
    print(f"  Image path: {img_path}")

    if not img_path:
        print("  No image found!")
        return

    # Load image
    content, filename, ext = get_image_content_and_filename(img_path)
    if not content:
        print("  Failed to load image!")
        return

    print(f"  Image loaded: {filename}.{ext}, {len(content)} bytes")
    print(f"  Type of content: {type(content)}")
    print(f"  First 4 bytes: {content[:4]}")

    # Get category ID
    cat_id = generate_uuid(f"category_{category_name}")

    # Delete old media first
    old_media_id = generate_uuid(f"category_media_{cat_id}")
    try:
        # Remove from category
        client.request_patch(f"category/{cat_id}", {"mediaId": None})
        print(f"  Removed old media reference from category")
    except Exception as e:
        print(f"  Could not remove media ref: {e}")

    try:
        client.request_delete(f"media/{old_media_id}")
        print(f"  Deleted old media {old_media_id}")
    except Exception as e:
        print(f"  Could not delete old media: {e}")

    # Create fresh media with new ID
    folder_id = get_or_create_media_folder(client, "Category Media")
    timestamp = int(time.time())
    fresh_media_id = hashlib.md5(f"category_media_{cat_id}_{timestamp}".encode()).hexdigest()
    unique_filename = f"{filename}-{timestamp}"

    print(f"\n  Creating new media: {fresh_media_id}")
    try:
        client.request_post("media", {"id": fresh_media_id, "mediaFolderId": folder_id})
        print(f"  Media entity created")
    except Exception as e:
        print(f"  Create media error: {e}")
        return

    # Upload binary content
    print(f"  Uploading {len(content)} bytes as {unique_filename}.{ext}...")
    try:
        client.request_post(
            f"_action/media/{fresh_media_id}/upload",
            payload=content,
            content_type="octet-stream",
            additional_query_params={"extension": ext, "fileName": unique_filename}
        )
        print(f"  Upload SUCCESS!")
    except Exception as e:
        print(f"  Upload FAILED: {e}")
        return

    # Assign to category
    try:
        client.request_patch(f"category/{cat_id}", {"mediaId": fresh_media_id})
        print(f"  Assigned media to category")
    except Exception as e:
        print(f"  Assign error: {e}")

    # Verify
    try:
        media = client.request_get(f"media/{fresh_media_id}")
        md = media.get("data", {})
        print(f"\n  Verification:")
        print(f"    fileName: {md.get('fileName')}")
        print(f"    fileSize: {md.get('fileSize')}")
        print(f"    mimeType: {md.get('mimeType')}")
        print(f"    url: {md.get('url')}")
    except Exception as e:
        print(f"  Verify error: {e}")

    return "Done"
