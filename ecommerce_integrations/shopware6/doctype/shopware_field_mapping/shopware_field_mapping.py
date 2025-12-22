# Copyright (c) 2024, Frappe Technologies and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class ShopwareFieldMapping(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		enabled: DF.Check
		erpnext_field: DF.Data
		filterable: DF.Check
		label_de: DF.Data | None
		label_en: DF.Data | None
		mapping_type: DF.Literal["Custom Field", "Property", "Standard Field"]
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		shopware_field: DF.Data
		shopware_field_type: DF.Literal["text", "html", "switch", "number", "select", "datetime"]
	# end: auto-generated types

	pass
