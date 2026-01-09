"""
Custom Fields for EU Tax Handling (Innergemeinschaftliche Lieferung)

This module creates custom fields required for:
- EU reverse charge handling
- VAT ID validation
- Tax exemption notices
- Delivery time calculation with working days
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def create_tax_custom_fields():
    """
    Create custom fields for EU tax handling on Sales Order and Sales Invoice.

    Fields created:
    - eu_reverse_charge: Flag for EU B2B reverse charge
    - tax_exemption_notice: Legal notice text for tax-exempt deliveries
    - validated_vat_id: Cached validated VAT ID
    - vat_validation_status: Status of VAT validation
    """
    custom_fields = {
        "Sales Order": [
            {
                "fieldname": "eu_reverse_charge",
                "label": "EU Reverse Charge (Innergemeinschaftliche Lieferung)",
                "fieldtype": "Check",
                "insert_after": "taxes_and_charges",
                "default": "0",
                "read_only": 1,
                "description": "Kennzeichnet innergemeinschaftliche Lieferungen mit Reverse Charge"
            },
            {
                "fieldname": "tax_exemption_notice",
                "label": "Steuerbefreiungshinweis",
                "fieldtype": "Small Text",
                "insert_after": "eu_reverse_charge",
                "read_only": 1,
                "description": "Gesetzlicher Hinweis für steuerbefreite Lieferungen (§4 Nr. 1b UStG, etc.)"
            },
            {
                "fieldname": "validated_vat_id",
                "label": "Validierte USt-IdNr.",
                "fieldtype": "Data",
                "insert_after": "tax_exemption_notice",
                "read_only": 1,
                "description": "Validierte EU-USt-IdNr. des Kunden"
            },
            {
                "fieldname": "vat_validation_status",
                "label": "USt-IdNr. Validierungsstatus",
                "fieldtype": "Select",
                "options": "Pending\nValid\nInvalid\nNot Required",
                "insert_after": "validated_vat_id",
                "read_only": 1,
                "description": "Status der USt-IdNr. Validierung gegen EU VIES"
            }
        ],
        "Sales Invoice": [
            {
                "fieldname": "eu_reverse_charge",
                "label": "EU Reverse Charge (Innergemeinschaftliche Lieferung)",
                "fieldtype": "Check",
                "insert_after": "taxes_and_charges",
                "default": "0",
                "read_only": 1,
                "description": "Kennzeichnet innergemeinschaftliche Lieferungen mit Reverse Charge"
            },
            {
                "fieldname": "tax_exemption_notice",
                "label": "Steuerbefreiungshinweis",
                "fieldtype": "Small Text",
                "insert_after": "eu_reverse_charge",
                "read_only": 1,
                "description": "Gesetzlicher Hinweis für steuerbefreite Lieferungen (§4 Nr. 1b UStG, etc.)"
            },
            {
                "fieldname": "validated_vat_id",
                "label": "Validierte USt-IdNr.",
                "fieldtype": "Data",
                "insert_after": "tax_exemption_notice",
                "read_only": 1,
                "description": "Validierte EU-USt-IdNr. des Kunden"
            },
            {
                "fieldname": "vat_validation_status",
                "label": "USt-IdNr. Validierungsstatus",
                "fieldtype": "Select",
                "options": "Pending\nValid\nInvalid\nNot Required",
                "insert_after": "validated_vat_id",
                "read_only": 1,
                "description": "Status der USt-IdNr. Validierung gegen EU VIES"
            }
        ]
    }

    create_custom_fields(custom_fields, update=True)
    frappe.db.commit()
    print("✅ Tax custom fields created for Sales Order and Sales Invoice")


def create_customer_vat_fields():
    """
    Create custom fields for VAT validation caching on Customer.

    Fields created:
    - vat_id_last_validated: Date of last VAT validation
    - vat_validation_result: Result of last validation
    """
    custom_fields = {
        "Customer": [
            {
                "fieldname": "vat_id_last_validated",
                "label": "USt-IdNr. zuletzt validiert am",
                "fieldtype": "Date",
                "insert_after": "tax_id",
                "read_only": 1,
                "description": "Datum der letzten USt-IdNr. Validierung gegen EU VIES"
            },
            {
                "fieldname": "vat_validation_result",
                "label": "USt-IdNr. Validierungsergebnis",
                "fieldtype": "Select",
                "options": "Valid\nInvalid\nNot Checked",
                "insert_after": "vat_id_last_validated",
                "read_only": 1,
                "description": "Ergebnis der letzten USt-IdNr. Validierung"
            }
        ]
    }

    create_custom_fields(custom_fields, update=True)
    frappe.db.commit()
    print("✅ VAT validation fields created for Customer")


def create_delivery_time_field():
    """
    Create custom field for German delivery time on Item.

    Field created:
    - custom_lieferzeit: German delivery time string (e.g., "3-5 Werktage", "2 Wochen")
    """
    custom_fields = {
        "Item": [
            {
                "fieldname": "custom_lieferzeit",
                "label": "Lieferzeit",
                "fieldtype": "Data",
                "insert_after": "weight_uom",
                "description": "Deutsche Lieferzeit (z.B. '3-5 Werktage', '2 Wochen', 'Sofort lieferbar'). "
                               "Wird für die Berechnung des Liefertermins verwendet."
            }
        ]
    }

    create_custom_fields(custom_fields, update=True)
    frappe.db.commit()
    print("✅ Delivery time field created for Item")


def create_all_custom_fields():
    """
    Create all custom fields required for EU tax handling and delivery time calculation.

    Run this function once during deployment:
    bench --site erp.example.com execute \
        ecommerce_integrations.shopware6.setup.custom_fields_tax.create_all_custom_fields
    """
    print("Creating custom fields for EU tax handling...")

    create_tax_custom_fields()
    create_customer_vat_fields()
    create_delivery_time_field()

    print("\n✅ All custom fields created successfully!")
    print("\nFields created:")
    print("  - Sales Order: eu_reverse_charge, tax_exemption_notice, validated_vat_id, vat_validation_status")
    print("  - Sales Invoice: eu_reverse_charge, tax_exemption_notice, validated_vat_id, vat_validation_status")
    print("  - Customer: vat_id_last_validated, vat_validation_result")
    print("  - Item: custom_lieferzeit")


def remove_all_custom_fields():
    """
    Remove all custom fields created by this module.

    Use this for cleanup during development/testing:
    bench --site erp.example.com execute \
        ecommerce_integrations.shopware6.setup.custom_fields_tax.remove_all_custom_fields
    """
    fields_to_remove = [
        ("Sales Order", "eu_reverse_charge"),
        ("Sales Order", "tax_exemption_notice"),
        ("Sales Order", "validated_vat_id"),
        ("Sales Order", "vat_validation_status"),
        ("Sales Invoice", "eu_reverse_charge"),
        ("Sales Invoice", "tax_exemption_notice"),
        ("Sales Invoice", "validated_vat_id"),
        ("Sales Invoice", "vat_validation_status"),
        ("Customer", "vat_id_last_validated"),
        ("Customer", "vat_validation_result"),
        ("Item", "custom_lieferzeit"),
    ]

    for dt, fieldname in fields_to_remove:
        try:
            custom_field = frappe.get_doc("Custom Field", f"{dt}-{fieldname}")
            custom_field.delete()
            print(f"✅ Removed {dt}.{fieldname}")
        except frappe.DoesNotExistError:
            print(f"⚠️  {dt}.{fieldname} does not exist")
        except Exception as e:
            print(f"❌ Error removing {dt}.{fieldname}: {e}")

    frappe.db.commit()
    print("\n✅ Custom fields removal completed")
