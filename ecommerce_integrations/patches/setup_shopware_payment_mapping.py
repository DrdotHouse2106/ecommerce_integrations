"""Add Payment Method Mapping table to Shopware Setting and pre-fill safe defaults.

Idempotent: skips rows that already exist on the setting, and only pre-fills a
row when the target ``Mode of Payment`` actually exists on the site (sites with
localised mode names get an empty table they can fill in via the UI).
"""

import frappe

from ecommerce_integrations.shopware6.constants import PAYMENT_METHOD_MAP


def execute():
	if not frappe.db.exists("DocType", "Shopware Setting"):
		return

	frappe.reload_doc("shopware6", "doctype", "shopware_payment_method_mapping")
	frappe.reload_doc("shopware6", "doctype", "shopware_setting")

	if not frappe.db.exists("Shopware Setting", "Shopware Setting"):
		return

	setting = frappe.get_doc("Shopware Setting", "Shopware Setting")
	existing = {
		(row.shopware_method or "").strip().lower()
		for row in (setting.payment_method_mappings or [])
	}

	appended = 0
	for shopware_key, mode_of_payment in PAYMENT_METHOD_MAP.items():
		key = shopware_key.strip().lower()
		if key in existing:
			continue
		if not frappe.db.exists("Mode of Payment", mode_of_payment):
			continue
		setting.append(
			"payment_method_mappings",
			{"shopware_method": key, "mode_of_payment": mode_of_payment},
		)
		existing.add(key)
		appended += 1

	if appended:
		setting.flags.ignore_mandatory = True
		setting.flags.ignore_permissions = True
		setting.save()
		print(f"Pre-filled {appended} payment method mappings on Shopware Setting")
