"""Add Payment Method Mapping table to Medusa Setting and pre-fill safe defaults.

Idempotent: skips rows that already exist on the setting, and only pre-fills
a row when the target ``Mode of Payment`` actually exists on the site.
"""

import frappe

from ecommerce_integrations.medusa.payment_method_mapping import (
	MEDUSA_PAYMENT_METHOD_MAP,
)


def execute():
	if not frappe.db.exists("DocType", "Medusa Setting"):
		return

	frappe.reload_doc("medusa", "doctype", "medusa_payment_method_mapping")
	frappe.reload_doc("medusa", "doctype", "medusa_setting")

	from ecommerce_integrations.medusa.custom_fields import setup_custom_fields
	setup_custom_fields()

	if not frappe.db.exists("Medusa Setting", "Medusa Setting"):
		return

	setting = frappe.get_doc("Medusa Setting", "Medusa Setting")
	existing = {
		(row.medusa_provider_id or "").strip().lower()
		for row in (setting.payment_method_mappings or [])
	}

	appended = 0
	for provider_id, mode_of_payment in MEDUSA_PAYMENT_METHOD_MAP.items():
		key = provider_id.strip().lower()
		if key in existing:
			continue
		if not frappe.db.exists("Mode of Payment", mode_of_payment):
			continue
		setting.append(
			"payment_method_mappings",
			{"medusa_provider_id": key, "mode_of_payment": mode_of_payment},
		)
		existing.add(key)
		appended += 1

	if appended:
		setting.flags.ignore_mandatory = True
		setting.flags.ignore_permissions = True
		setting.save()
		print(f"Pre-filled {appended} payment method mappings on Medusa Setting")
