# Copyright (c) 2024, Frappe and contributors
# For license information, please see license.txt

"""
Patch to set up AI Description custom fields on Item doctype.

This patch creates all custom fields needed for AI-generated product descriptions.
"""

import frappe


def execute():
    """Set up AI Description custom fields"""
    try:
        from ecommerce_integrations.ai_description.custom_fields import setup_custom_fields
        setup_custom_fields()
        frappe.db.commit()
    except Exception as e:
        frappe.log_error(
            title="AI Description Custom Fields Setup Failed",
            message=str(e)
        )
