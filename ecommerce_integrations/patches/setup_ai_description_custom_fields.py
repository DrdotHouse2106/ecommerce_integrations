# Copyright (c) 2024, Frappe and contributors
# For license information, please see license.txt

"""
Patch to set up AI Description custom fields on Item doctype.

This patch creates all custom fields needed for AI-generated product descriptions.

Previously wrapped in a try/except that logged and swallowed any
failure — meaning a genuine field-creation bug would still mark this
patch as done in the Patch Log, silently leaving Items without the
AI-description fields with no way to detect it short of noticing the
missing fields on the form. Letting it raise is consistent with every
other patch in this app: a failed patch stops the migrate so the
operator actually sees the error.
"""

import frappe


def execute():
    """Set up AI Description custom fields"""
    from ecommerce_integrations.ai_description.custom_fields import setup_custom_fields
    setup_custom_fields()
    frappe.db.commit()
