"""Adds ``Ecommerce Channel Branding.versandabsender`` — a per-channel Link
to the ``Versandabsender`` doctype shipped by a separately installed
shipping-label app (see the README's "Shipping label integration" note).

No-op wherever that app isn't installed: the target doctype doesn't
exist, so there's nothing to link to and the field would be dead weight.
This keeps the plugin generically installable on its own; operators who
also run the shipping-label app get the field automatically on their
next migrate.

Idempotent: ``create_custom_fields`` upserts by fieldname.

Runs in patches.txt's ``[post_model_sync]`` section, not the default
pre-model-sync section. ``create_custom_fields`` triggers a full
``validate_fields_for_doctype`` pass on ``Ecommerce Channel Branding``
as a side effect of saving the new Custom Field — on a site whose DB
still has a stale invalid field definition from before it was fixed in
this app's own doctype JSON (e.g. an ``Autocomplete`` field with
``unique=1``, which Frappe rejects), that validation fails. Pre-model-
sync patches run *before* this app's doctype JSON changes are synced
into the DB, so the stale definition is still live at that point;
post-model-sync patches run *after*, once the DB matches the current
JSON.
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
