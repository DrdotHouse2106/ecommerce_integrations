# Copyright (c) 2024, Frappe Technologies and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class ItemShopwareProperty(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		filterable: DF.Check
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		property_name: DF.Data
		property_type: DF.Literal["Property", "Custom Field"]
		property_value: DF.SmallText
		shopware_field_type: DF.Literal["text", "html", "switch", "number", "select", "datetime"]
	# end: auto-generated types

	pass
