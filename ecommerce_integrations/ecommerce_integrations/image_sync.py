"""Re-queue parent Items for resync when attached image Files change.

The ``Item.on_update`` hook fans out to per-channel bulk-sync queues, but it
only fires when the Item document itself is modified. Adding, replacing or
deleting gallery image attachments creates/deletes ``File`` records pointing
at the Item without touching the Item doc — those changes silently miss the
next sync cycle. This module bridges that gap.
"""

import frappe

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".avif")

# (label, dotted path to queue_item_for_sync). Adding a new channel is one
# tuple here; the dispatch loop below picks it up automatically.
_CHANNEL_QUEUES = (
	("Shopware", "ecommerce_integrations.shopware6.bulk_sync.queue_item_for_sync"),
	("Medusa", "ecommerce_integrations.medusa.bulk_sync.queue_item_for_sync"),
)


def _is_image_attachment(doc) -> bool:
	if doc.get("is_folder"):
		return False
	if doc.get("attached_to_doctype") != "Item":
		return False
	if not doc.get("attached_to_name"):
		return False
	file_url = (doc.get("file_url") or "").lower()
	if not file_url:
		return False
	return file_url.endswith(IMAGE_EXTS)


def queue_parent_item_for_sync(doc, method=None):
	if not _is_image_attachment(doc):
		return

	try:
		item = frappe.get_doc("Item", doc.attached_to_name)
	except frappe.DoesNotExistError:
		return

	# Per-channel try/except: a misconfigured integration must not block File CRUD.
	for label, dotted_path in _CHANNEL_QUEUES:
		try:
			frappe.get_attr(dotted_path)(item, "on_update")
		except Exception:
			frappe.log_error(
				title=f"image_sync: {label} queue failed",
				message=frappe.get_traceback(),
			)
