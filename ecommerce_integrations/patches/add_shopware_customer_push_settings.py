"""Reload the ``Shopware Setting`` doctype on existing sites so the new
"Kunden nach Shopware übertragen" section (``enable_customer_push``,
``customer_number_counter_seed``, ``shopware_default_customer_group_name``,
``shopware_default_payment_method_name``, ``shopware_default_salutation_key``)
reaches sites installed before this feature landed.

No-op safe: only runs when the Shopware Setting doctype exists on this site.
``frappe.reload_doc`` is idempotent — it re-syncs the DocType definition from
disk regardless of how many times it's called.
"""

import frappe


def execute() -> None:
	if not frappe.db.exists("DocType", "Shopware Setting"):
		return
	frappe.reload_doc("shopware6", "doctype", "shopware_setting")
