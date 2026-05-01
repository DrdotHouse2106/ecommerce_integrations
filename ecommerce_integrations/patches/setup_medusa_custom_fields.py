import frappe


def execute():
	if frappe.db.exists("DocType", "Medusa Setting"):
		from ecommerce_integrations.medusa.custom_fields import setup_custom_fields
		setup_custom_fields()
