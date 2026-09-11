"""Optional bridge to a separately-installed shipping-label app's
``Versandabsender`` doctype (custom field ``Sales Order.vi_versandabsender``).

Fully no-op wherever that app isn't installed, so this plugin stays
generic-installable on its own — see the README for why combining both
apps is worthwhile.

The shop channel that created the order takes priority over that app's
own Customer-based ``fetch_if_empty`` default: setting the field here,
before insert, means the Customer default never fires for this order.
Orders imported without a channel mapping configured are left alone, so
they still fall through to that Customer default unchanged — this
module only ever sets the field, never clears it.
"""

import frappe

VERSANDABSENDER_FIELD = "vi_versandabsender"


def resolve_channel_versandabsender(channel_name: str) -> str | None:
	"""Versandabsender configured on the channel's Ecommerce Channel
	Branding record, or None if unset, the branding record doesn't
	exist, or the shipping-label app isn't installed (in which case the
	custom field itself doesn't exist and this is a cheap no-op)."""
	if not channel_name:
		return None
	if not frappe.db.has_column("Ecommerce Channel Branding", "versandabsender"):
		return None
	return frappe.db.get_value("Ecommerce Channel Branding", channel_name, "versandabsender")
