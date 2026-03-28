"""Shared utilities for the universal ecommerce_properties child table."""

from frappe.utils import cstr


def get_ecommerce_properties(item, sync_flag="sync_to_shopware"):
	"""Return ecommerce_properties rows filtered by a sync flag.

	Args:
		item: ERPNext Item document (or dict-like with child table)
		sync_flag: Field name to filter on ('sync_to_shopware' or 'sync_to_medusa')
	"""
	return [
		row for row in (getattr(item, "ecommerce_properties", None) or [])
		if getattr(row, sync_flag, 0)
	]


def shopware_custom_field_name(property_name: str) -> str:
	"""Derive the Shopware custom field name from a property name.

	Example: 'is_surcharge' -> 'erpnext_is_surcharge'
	"""
	return f"erpnext_{property_name.lower().replace(' ', '_')}"


def coerce_custom_field_value(value: str):
	"""Convert a string property value to the appropriate Python type.

	Boolean strings ('true', '1', 'yes') become True/False,
	everything else stays as-is.
	"""
	v = value.lower()
	if v in ("true", "1", "yes"):
		return True
	if v in ("false", "0", "no"):
		return False
	return value
