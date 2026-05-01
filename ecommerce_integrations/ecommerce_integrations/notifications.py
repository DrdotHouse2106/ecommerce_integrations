"""Channel-aware email sender overrides for notifications.

When an ecommerce notification fires, we resolve the branding for the doc's
sales channel and override the Notification's sender with the channel's
configured outgoing email account.

Wired up via hooks.py → doc_events → Sales Order / Delivery Note / Sales Invoice
before_send hook does not exist, so we use a Notification send hook instead.
"""

import frappe

from ecommerce_integrations.ecommerce_integrations.doctype.ecommerce_channel_branding.ecommerce_channel_branding import (
	get_branding_for_doc,
)


def get_notification_sender_for_doc(doc) -> str:
	"""Resolve which Email Account should send notifications for this doc.

	Returns the Email Account name, or "" if no channel-specific account is configured.
	"""
	try:
		brand = get_branding_for_doc(doc)
	except Exception:
		return ""
	return brand.get("outgoing_email_account") or ""
