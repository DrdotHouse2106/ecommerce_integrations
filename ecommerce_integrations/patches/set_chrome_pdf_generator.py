"""Switch ecommerce print formats to the Chrome PDF generator.

wkhtmltopdf fetches embedded assets (logos, web fonts, …) by HTTP back to the
site and fails with HostNotFoundError when the container can't resolve its own
hostname. Frappe v16 ships an in-process Chrome-based generator that renders
without those outbound HTTP calls — but ``Print Format.pdf_generator``
defaults to ``wkhtmltopdf``, so we have to opt-in per format.

Idempotent: skips when the pdf_generator field doesn't exist (older Frappe)
or when the format is already on Chrome.
"""

import frappe


PRINT_FORMATS = ("Bestellbestätigung", "Auftragsbestätigung", "Versandbestätigung")


def execute():
    meta = frappe.get_meta("Print Format")
    if not meta.has_field("pdf_generator"):
        return

    for pf_name in PRINT_FORMATS:
        if not frappe.db.exists("Print Format", pf_name):
            continue
        current = frappe.db.get_value("Print Format", pf_name, "pdf_generator")
        if current == "chrome":
            continue
        frappe.db.set_value(
            "Print Format", pf_name, "pdf_generator", "chrome", update_modified=False,
        )
