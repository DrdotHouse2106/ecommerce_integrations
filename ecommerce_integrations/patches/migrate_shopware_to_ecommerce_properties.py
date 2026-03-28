"""Migrate Item Shopware Property data to Item Ecommerce Property.

Copies all rows via bulk INSERT, maps shopware_field_type -> field_data_type
(including switch -> boolean), sets sync_to_shopware=1 on all migrated rows.
Does NOT delete the old table (kept for rollback safety).
"""

import frappe


FIELD_TYPE_MAP = {
	"switch": "boolean",
}


def execute():
	if not frappe.db.table_exists("tabItem Shopware Property"):
		return

	if not frappe.db.table_exists("tabItem Ecommerce Property"):
		return

	if frappe.db.count("Item Ecommerce Property") > 0:
		return

	source_count = frappe.db.count("Item Shopware Property")
	if not source_count:
		return

	frappe.db.sql("""
		INSERT INTO `tabItem Ecommerce Property`
			(name, parent, parentfield, parenttype, idx,
			 property_name, property_value, property_type,
			 field_data_type, filterable,
			 sync_to_shopware, sync_to_medusa,
			 creation, modified, modified_by, owner, docstatus)
		SELECT
			REPLACE(UUID(), '-', ''),
			parent, 'ecommerce_properties', parenttype, idx,
			property_name, property_value, property_type,
			CASE WHEN shopware_field_type = 'switch' THEN 'boolean'
			     WHEN shopware_field_type IS NULL THEN 'text'
			     ELSE shopware_field_type END,
			COALESCE(filterable, 0),
			1, 0,
			NOW(), NOW(), 'Administrator', 'Administrator', 0
		FROM `tabItem Shopware Property`
	""")

	migrated = frappe.db.count("Item Ecommerce Property")
	print(f"Migrated {migrated}/{source_count} properties from Item Shopware Property -> Item Ecommerce Property")
