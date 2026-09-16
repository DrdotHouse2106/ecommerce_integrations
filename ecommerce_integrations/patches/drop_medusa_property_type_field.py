"""Drop ``Item Attribute.medusa_property_type`` — dead field.

Option-vs-metadata routing for Medusa properties has always been
decided by ``Item Ecommerce Property.property_type``/``filterable``
(see ``medusa/product_export.py``'s ``_get_medusa_metadata``/
``_collect_attribute_entries``, and ``canonical.py``'s
``_canonical_properties`` for the modern engine) — never by this
Select field on Item Attribute, which was defined and shown on the
form but never read anywhere.

No data migration: the field never had a working consumer, so there's
nothing meaningful to preserve before dropping it.
"""

import frappe


def execute() -> None:
	if not frappe.db.exists("DocType", "Item Attribute"):
		return
	if not frappe.db.exists("Custom Field", "Item Attribute-medusa_property_type"):
		return

	frappe.delete_doc("Custom Field", "Item Attribute-medusa_property_type", ignore_permissions=True)
	frappe.db.commit()  # noqa: SLF001
