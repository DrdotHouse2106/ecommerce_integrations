"""
Custom Fields for AI Description Generation

Defines custom fields added to Item doctype for AI-generated product descriptions.
"""

import frappe

CUSTOM_FIELDS = {
    "Item": [
        # AI Description Section
        {
            "fieldname": "ai_description_section",
            "label": "AI-Generated Description",
            "fieldtype": "Section Break",
            "insert_after": "description",
            "collapsible": 1,
            "collapsible_depends_on": "eval:!doc.ai_description_generated"
        },
        {
            "fieldname": "ai_short_description",
            "label": "Short Description (Hook)",
            "fieldtype": "Small Text",
            "insert_after": "ai_description_section",
            "description": "1-2 sentences highlighting the main benefit"
        },
        {
            "fieldname": "ai_benefits",
            "label": "Benefits (JSON)",
            "fieldtype": "Code",
            "options": "JSON",
            "insert_after": "ai_short_description",
            "description": "Array of benefit strings"
        },
        {
            "fieldname": "ai_long_description",
            "label": "Long Description (HTML)",
            "fieldtype": "Text Editor",
            "insert_after": "ai_benefits",
            "description": "Detailed product description with HTML formatting"
        },
        {
            "fieldname": "ai_column_break",
            "fieldtype": "Column Break",
            "insert_after": "ai_long_description"
        },
        {
            "fieldname": "ai_applications",
            "label": "Application Areas",
            "fieldtype": "Small Text",
            "insert_after": "ai_column_break",
            "description": "Comma-separated list of application areas"
        },
        {
            "fieldname": "ai_delivery_scope",
            "label": "Delivery Scope (JSON)",
            "fieldtype": "Code",
            "options": "JSON",
            "insert_after": "ai_applications",
            "description": "Array of items included in delivery"
        },
        # SEO Section (if not already existing)
        {
            "fieldname": "ai_seo_section",
            "label": "AI-Generated SEO",
            "fieldtype": "Section Break",
            "insert_after": "ai_delivery_scope",
            "collapsible": 1
        },
        {
            "fieldname": "ai_seo_title",
            "label": "SEO Title",
            "fieldtype": "Data",
            "insert_after": "ai_seo_section",
            "length": 70,
            "description": "Max 60 characters for optimal display in search results"
        },
        {
            "fieldname": "ai_seo_description",
            "label": "SEO Meta Description",
            "fieldtype": "Small Text",
            "insert_after": "ai_seo_title",
            "description": "Max 155 characters for optimal display in search results"
        },
        # AI Metadata Section
        {
            "fieldname": "ai_metadata_section",
            "label": "AI Generation Metadata",
            "fieldtype": "Section Break",
            "insert_after": "ai_seo_description",
            "collapsible": 1
        },
        {
            "fieldname": "ai_description_generated",
            "label": "AI Description Generated",
            "fieldtype": "Check",
            "insert_after": "ai_metadata_section",
            "read_only": 1,
            "default": 0
        },
        {
            "fieldname": "ai_generation_date",
            "label": "Generated On",
            "fieldtype": "Datetime",
            "insert_after": "ai_description_generated",
            "read_only": 1
        },
        {
            "fieldname": "ai_column_break_meta",
            "fieldtype": "Column Break",
            "insert_after": "ai_generation_date"
        },
        {
            "fieldname": "ai_model_used",
            "label": "AI Model Used",
            "fieldtype": "Data",
            "insert_after": "ai_column_break_meta",
            "read_only": 1
        },
        {
            "fieldname": "ai_description_reviewed",
            "label": "Reviewed by Human",
            "fieldtype": "Check",
            "insert_after": "ai_model_used",
            "default": 0,
            "description": "Check after reviewing and approving the AI-generated content"
        }
    ]
}


def setup_custom_fields():
    """Create all custom fields for AI Description integration."""
    from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

    # Check if fields already exist before creating
    for doctype, fields in CUSTOM_FIELDS.items():
        for field in fields:
            fieldname = field.get("fieldname")
            existing = frappe.db.exists("Custom Field", {"dt": doctype, "fieldname": fieldname})
            if existing:
                frappe.logger().info(f"Custom field {doctype}-{fieldname} already exists, skipping")

    create_custom_fields(CUSTOM_FIELDS)
    frappe.db.commit()


def remove_custom_fields():
    """Remove custom fields (for uninstall)."""
    for doctype, fields in CUSTOM_FIELDS.items():
        for field in fields:
            fieldname = field.get("fieldname")
            if frappe.db.exists("Custom Field", {"dt": doctype, "fieldname": fieldname}):
                frappe.delete_doc("Custom Field", f"{doctype}-{fieldname}")
