"""Install ecommerce channel-tracking custom fields on Sales Invoice / Delivery
Note / Payment Entry.

These fields (``ecommerce_source``, ``ecommerce_sales_channel_id``,
``ecommerce_sales_channel_name``) were added later to the medusa custom_fields
config — re-running ``setup_custom_fields()`` picks them up. Idempotent
because ``create_custom_fields`` is called with ``update=True``.
"""

import frappe


def execute():
	if not frappe.db.exists("DocType", "Medusa Setting"):
		return
	from ecommerce_integrations.medusa.custom_fields import setup_custom_fields
	setup_custom_fields()
