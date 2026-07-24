"""
Custom Fields for Shopware 6 Integration

Defines custom fields added to standard ERPNext doctypes.
"""

import frappe

from ecommerce_integrations.ecommerce_integrations.ecommerce_custom_fields import ECOMMERCE_SALES_ORDER_FIELDS

CUSTOM_FIELDS = {
    "Item Group": [
        {
            "fieldname": "shopware_section",
            "label": "Shopware-Einstellungen",
            "fieldtype": "Section Break",
            "insert_after": "is_group",
            "collapsible": 1,
        },
        {
            "fieldname": "shopware_active",
            "label": "Aktiv in Shopware",
            "fieldtype": "Check",
            "insert_after": "shopware_section",
            "default": 1,
            "description": "Wenn deaktiviert, wird diese Kategorie in Shopware auf inaktiv gesetzt (für Kunden unsichtbar). Betrifft nur die Sichtbarkeit in Shopware, nicht ERPNext.",
        },
        {
            "fieldname": "shopware_priority",
            "label": "Priorität (Shopware)",
            "fieldtype": "Int",
            "insert_after": "shopware_active",
            "default": "0",
            "description": "Sortierpriorität dieser Kategorie in Shopware. Niedrigere Zahlen erscheinen zuerst. Wird vom Frontend zur Sortierung verwendet.",
        },
        {
            "fieldname": "category_image",
            "label": "Kategoriebild (Shopware)",
            "fieldtype": "Attach Image",
            "insert_after": "shopware_priority",
            "description": "Bild, das für diese Kategorie in Shopware angezeigt wird. Wenn leer, wird das Standard-Artikelgruppenbild verwendet.",
        },
        {
            "fieldname": "seo_title",
            "label": "SEO-Titel (Shopware)",
            "fieldtype": "Data",
            "insert_after": "category_image",
            "description": "Meta-Titel, der an Shopware übertragen wird. Leer lassen, um den Kategorienamen zu verwenden.",
        },
        {
            "fieldname": "seo_meta_description",
            "label": "SEO-Meta-Beschreibung (Shopware)",
            "fieldtype": "Small Text",
            "insert_after": "seo_title",
        },
        {
            "fieldname": "seo_keywords",
            "label": "SEO-Schlüsselwörter (Shopware)",
            "fieldtype": "Data",
            "insert_after": "seo_meta_description",
        },
    ],
    "Item": [
        {
            "fieldname": "shopware_selling_rate",
            "label": "Shopware Selling Rate",
            "fieldtype": "Currency",
            "insert_after": "standard_rate",
            "read_only": 0,
            "translatable": 0,
        },
        # Note: SEO fields (seo_title, seo_meta_description, seo_keywords, delivery_time)
        # are expected to already exist in ERPNext Item doctype.
        # They are synced to Shopware via product_export.py

        # Shopware Properties Table (Key-Value pairs for flexible property management)
        # Replaces old individual custom fields (shopware_zubehoer, jattr_*, etc.)
        {
            "fieldname": "shopware_topseller",
            "label": "Topseller (Shopware)",
            "fieldtype": "Check",
            "insert_after": "shopware_selling_rate",
            "default": "0",
            "description": "Markiert dieses Produkt als Topseller im Shop (gelbes 'Tipp'-Badge)",
        },
        {
            "fieldname": "shopware_description_override",
            "label": "Beschreibung überschreiben (Shopware)",
            "fieldtype": "Text Editor",
            "insert_after": "shopware_topseller",
            "description": (
                "Optionale Beschreibung, die nur beim Shopware-Produkt-Push verwendet wird "
                "und Vorrang vor KI-/Web-Beschreibung sowie der Standard-Artikelbeschreibung hat. "
                "Leer lassen, um die normale Beschreibung zu verwenden."
            ),
        },
        {
            "fieldname": "delivery_time",
            "label": "Lieferzeit (Shopware)",
            "fieldtype": "Data",
            "insert_after": "shopware_description_override",
            "description": (
                "Freitext-Lieferzeit für Shopware, z. B. '3-5 Tage' oder '1-2 Wochen'. "
                "Wird automatisch in eine Shopware-Lieferzeit-Entität umgewandelt. Leer = "
                "keine Lieferzeit am Produkt gesetzt."
            ),
        },
        # SEO: no new fields here — the WeClapp import already brought
        # metatitel / metabeschreibung / metakeywords onto Item; the
        # Shopware push reads those directly (see product_mapper.py).
        {
            "fieldname": "ecommerce_properties_section",
            "label": "Ecommerce Properties",
            "fieldtype": "Section Break",
            "insert_after": "shopware_selling_rate",
            "collapsible": 0,
            "description": "Universal property table for all ecommerce integrations (Shopware, Medusa, etc.)",
        },
        {
            "fieldname": "ecommerce_properties",
            "label": "Properties",
            "fieldtype": "Table",
            "options": "Item Ecommerce Property",
            "insert_after": "ecommerce_properties_section",
            "description": "Add properties here. Enable sync checkboxes to control which integrations receive each property.",
        },
        # Multi-Storefront / Sales Channel settings
        {
            "fieldname": "shopware_channels_section",
            "label": "Shopware Sales Channels",
            "fieldtype": "Section Break",
            "insert_after": "ecommerce_properties",
            "collapsible": 1,
            "description": "Override which Sales Channels this product is visible in",
        },
        {
            "fieldname": "shopware_use_item_group_channels",
            "label": "Use Item Group Channels",
            "fieldtype": "Check",
            "insert_after": "shopware_channels_section",
            "default": "1",
            "description": "If checked, use the channel assignment from Item Group. Uncheck to override.",
        },
        {
            "fieldname": "shopware_all_channels",
            "label": "Visible in ALL Channels",
            "fieldtype": "Check",
            "insert_after": "shopware_use_item_group_channels",
            "default": "0",
            "depends_on": "eval:!doc.shopware_use_item_group_channels",
            "description": "Make this product visible in every Sales Channel",
        },
        {
            "fieldname": "shopware_channel_overrides",
            "label": "Channel Overrides",
            "fieldtype": "Table",
            "options": "Item Sales Channel Override",
            "insert_after": "shopware_all_channels",
            "depends_on": "eval:!doc.shopware_use_item_group_channels && !doc.shopware_all_channels",
            "description": "Specify exactly which channels this product should appear in",
        },
    ],
    "Customer": [
        {
            "fieldname": "shopware_customer_id",
            "label": "Shopware Customer ID",
            "fieldtype": "Data",
            "insert_after": "customer_name",
            "read_only": 1,
            "translatable": 0,
        },
        {
            "fieldname": "invoice_email",
            "label": "Invoice Email",
            "fieldtype": "Data",
            "insert_after": "email_id",
            "read_only": 0,
            "translatable": 0,
            "options": "Email",
            "description": "Alternative email address for invoices (from Shopware checkout)",
        },
        # Multi-Storefront: Track which Sales Channel the customer came from
        {
            "fieldname": "shopware_source_sales_channel_id",
            "label": "Source Sales Channel ID",
            "fieldtype": "Data",
            "insert_after": "shopware_customer_id",
            "read_only": 1,
            "translatable": 0,
            "hidden": 1,
        },
        {
            "fieldname": "shopware_source_sales_channel_name",
            "label": "Source Sales Channel",
            "fieldtype": "Data",
            "insert_after": "shopware_source_sales_channel_id",
            "read_only": 1,
            "translatable": 0,
            "description": "Which shop this customer first registered from",
        },
        # XRechnung / B2G snapshot fields. The Shopware Plugin X stamps
        # ``leitweg_id`` + ``is_government_org`` onto every order coming
        # from a public-sector buyer; we mirror them onto the Customer
        # master via the ``Ecommerce Checkout Field`` mappings so the
        # values persist beyond the single order and stay queryable on
        # the customer card.
        #
        # The values are intentionally a *snapshot* — Alyf's
        # ``eu_einvoice`` reads from ``Customer.electronic_address`` +
        # ``Customer.electronic_address_scheme = '0204'`` for the actual
        # XRechnung BT-49 render. Mirroring ``leitweg_id`` into Alyf's
        # field is handled by a ``Customer.before_save`` hook in
        # ``shopware6/customer/sync.py`` so the operator's Leitweg-ID
        # in this friendly field automatically populates the EN-16931
        # path too.
        {
            "fieldname": "leitweg_id",
            "label": "Leitweg-ID",
            "fieldtype": "Data",
            "insert_after": "tax_id",
            "translatable": 0,
            "description": "B2G routing identifier (Format 991-12345-67). Auto-mirrored to electronic_address + EAS scheme 0204 for XRechnung BT-49.",
        },
        {
            "fieldname": "is_government_org",
            "label": "Öffentlicher Auftraggeber",
            "fieldtype": "Check",
            "insert_after": "leitweg_id",
            "description": "Tick when this customer is a public-sector entity. Influences einvoice_profile selection on Sales Invoice.",
        },
    ],
    "Sales Order": [
        {
            "fieldname": "shopware_section",
            "label": "Shopware 6",
            "fieldtype": "Section Break",
            "insert_after": "tax_id",
            "collapsible": 1,
        },
        {
            "fieldname": "shopware_order_id",
            "label": "Shopware Order ID",
            "fieldtype": "Data",
            "insert_after": "shopware_section",
            "read_only": 1,
            "translatable": 0,
        },
        {
            "fieldname": "shopware_order_number",
            "label": "Shopware Order Number",
            "fieldtype": "Data",
            "insert_after": "shopware_order_id",
            "read_only": 1,
            "translatable": 0,
            "in_global_search": 1,
            "in_standard_filter": 1,
            "description": (
                "Wird auch als Name/ID des Verkaufsauftrags selbst verwendet, wenn bei "
                "Sales Order als Auto Name 'Prompt' eingestellt ist (siehe order_sync.create_sales_order)."
            ),
        },
        {
            "fieldname": "shopware_column_break",
            "label": "",
            "fieldtype": "Column Break",
            "insert_after": "shopware_order_number",
        },
        {
            "fieldname": "shopware_payment_method",
            "label": "Shopware Payment Method",
            "fieldtype": "Data",
            "insert_after": "shopware_column_break",
            "read_only": 1,
            "translatable": 0,
            "description": "Original payment method from Shopware",
        },
        {
            "fieldname": "shopware_payment_status",
            "label": "Shopware Payment Status",
            "fieldtype": "Data",
            "insert_after": "shopware_payment_method",
            "read_only": 1,
            "translatable": 0,
            "description": "Payment status from Shopware (Paid, Unpaid, etc.)",
        },
        {
            "fieldname": "shopware_erpnext_mode_of_payment",
            "label": "Mode of Payment (Mapped)",
            "fieldtype": "Link",
            "options": "Mode of Payment",
            "insert_after": "shopware_payment_status",
            "read_only": 1,
            "translatable": 0,
            "description": "ERPNext Mode of Payment mapped from Shopware payment method",
        },
        # PSP reconciliation: provider + the provider's transaction id, captured
        # from the Shopware order transaction's customFields (Shopware does not
        # persist the fee). Generic — lets any operator pull the exact PSP fee
        # from the provider API for gross-method booking.
        {
            "fieldname": "psp_section",
            "label": "PSP / Zahlungsabgleich",
            "fieldtype": "Section Break",
            "insert_after": "shopware_erpnext_mode_of_payment",
            "collapsible": 1,
        },
        {
            "fieldname": "psp_provider",
            "label": "PSP Provider",
            "fieldtype": "Data",
            "insert_after": "psp_section",
            "read_only": 1,
            "translatable": 0,
            "description": "Normalised payment provider (PayPal, Stripe, Klarna, …).",
        },
        {
            "fieldname": "psp_transaction_id",
            "label": "PSP Transaction ID",
            "fieldtype": "Data",
            "insert_after": "psp_provider",
            "read_only": 1,
            "translatable": 0,
            "description": "Provider transaction id (e.g. PayPal capture id) for fee/payout lookup.",
        },
        {
            "fieldname": "psp_order_reference",
            "label": "PSP Order Reference",
            "fieldtype": "Data",
            "insert_after": "psp_transaction_id",
            "read_only": 1,
            "translatable": 0,
        },
        {
            "fieldname": "psp_payment_handler",
            "label": "PSP Payment Handler",
            "fieldtype": "Data",
            "insert_after": "psp_order_reference",
            "read_only": 1,
            "translatable": 0,
            "hidden": 1,
        },
        {
            "fieldname": "psp_is_sandbox",
            "label": "PSP Sandbox",
            "fieldtype": "Check",
            "insert_after": "psp_payment_handler",
            "read_only": 1,
        },
        {
            "fieldname": "psp_captured_amount",
            "label": "PSP Captured Amount",
            "fieldtype": "Currency",
            "insert_after": "psp_is_sandbox",
            "read_only": 1,
            "description": "Gross amount actually captured by the PSP (for exact "
            "clearing booking on partial captures / rounding).",
        },
        *ECOMMERCE_SALES_ORDER_FIELDS,
    ],
    "Delivery Note": [
        {
            "fieldname": "shopware_delivery_id",
            "label": "Shopware Delivery ID",
            "fieldtype": "Data",
            "insert_after": "title",
            "read_only": 1,
            "translatable": 0,
        },
    ],
    "Sales Invoice": [
        {
            "fieldname": "shopware_transaction_id",
            "label": "Shopware Transaction ID",
            "fieldtype": "Data",
            "insert_after": "title",
            "read_only": 1,
            "translatable": 0,
        },
    ],
}


def setup_custom_fields():
    """Create all custom fields for Shopware integration.

    ``update=True`` so re-running this (e.g. via
    ``patches.add_shopware_description_override_field``) also pushes
    property changes on already-installed fields — not just missing ones.
    """
    from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

    create_custom_fields(CUSTOM_FIELDS, update=True)


def remove_custom_fields():
    """Remove custom fields (for uninstall)."""
    for doctype, fields in CUSTOM_FIELDS.items():
        for field in fields:
            fieldname = field.get("fieldname")
            if frappe.db.exists("Custom Field", {"dt": doctype, "fieldname": fieldname}):
                frappe.delete_doc("Custom Field", f"{doctype}-{fieldname}")
