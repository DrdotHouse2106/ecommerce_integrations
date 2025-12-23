import frappe


def before_uninstall():
	# This large table is linked with "modules" hence gets deleted one by one
	frappe.db.delete("Ecommerce Integration Log")

	# Clean up AI Description logs
	if frappe.db.table_exists("AI Description Log"):
		frappe.db.delete("AI Description Log")

	# Remove AI Description custom fields
	try:
		from ecommerce_integrations.ai_description.custom_fields import remove_custom_fields
		remove_custom_fields()
	except Exception:
		pass
