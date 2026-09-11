"""Adds ``Ecommerce Channel Branding.versandabsender`` — a per-channel Link
to the ``Versandabsender`` doctype shipped by a separately installed
shipping-label app (see the README's "Shipping label integration" note).

No-op wherever that app isn't installed: the target doctype doesn't
exist, so there's nothing to link to and the field would be dead weight.
This keeps the plugin generically installable on its own; operators who
also run the shipping-label app get the field automatically on their
next migrate.

Idempotent: ``create_custom_fields`` upserts by fieldname.
"""

import frappe


def execute() -> None:
	if not frappe.db.exists("DocType", "Versandabsender"):
		return
	if not frappe.db.exists("DocType", "Ecommerce Channel Branding"):
		return

	from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

	create_custom_fields(
		{
			"Ecommerce Channel Branding": [
				{
					"fieldname": "versandabsender",
					"label": "Versandabsender",
					"fieldtype": "Link",
					"options": "Versandabsender",
					"insert_after": "shop_url",
					"description": (
						"Absender für Versandetiketten dieses Kanals (aus der "
						"Versand-App). Wird beim Bestellimport auf den Sales "
						"Order gesetzt und hat Vorrang vor der kundenbasierten "
						"Standardbelegung dieser App."
					),
				},
			],
		},
		update=True,
	)
