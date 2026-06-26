"""Override Frappe's Notification class to use channel-specific email settings.

When a notification fires for a Sales Order / Delivery Note / Sales Invoice
that has an ecommerce_sales_channel_name, we look up the channel's
Ecommerce Channel Branding and override:
  - sender (Email Account) — routed by doctype: invoice-related docs use
    ``outgoing_email_account_invoice``, everything else (orders, delivery notes)
    uses ``outgoing_email_account``. Each falls back to the other if empty.
  - sender_full_name
  - bcc (append channel-specific BCC)
  - subject — when the branding has a non-empty per-kind subject field
    (``email_acknowledgment_subject`` / ``email_confirmation_subject`` /
    ``email_invoice_subject``), it replaces the notification's default subject.
    Mapping is keyed by Notification name in ``SUBJECT_FIELD_BY_NOTIFICATION``.
  - language (frappe.local.lang + doc.language for both subject/body
    rendering and PDF attachment)
  - E-Invoice (when ``enable_einvoice`` is on AND doc is a Sales Invoice):
    embed the XRechnung/ZUGFeRD XML into the PDF (via eu_einvoice's
    ``attach_xml_to_pdf``) and additionally attach the raw XML as a separate
    file. Both happen by wrapping ``frappe.attach_print`` for the duration of
    the send and by extending ``get_attachment`` to include the XML file.

Registered via `override_doctype_class` in hooks.py.
"""

import frappe
from frappe.email.doctype.notification.notification import Notification

from ecommerce_integrations.ecommerce_integrations.doctype.ecommerce_channel_branding.ecommerce_channel_branding import (
	get_branding,
)


_UNSET = object()

# Documents that should be sent through the invoice email account.
INVOICE_DOCTYPES = frozenset({
	"Sales Invoice",
	"Payment Entry",
	"Payment Request",
	"Dunning",
	"Credit Note",
})

# Map shipped Notification names to the branding field that overrides their subject.
# A non-empty value on the branding doc replaces the notification's subject template,
# so each channel can phrase its own subject (still rendered as Jinja against `doc`).
SUBJECT_FIELD_BY_NOTIFICATION = {
	"Ecommerce Order Acknowledgment": "email_acknowledgment_subject",
	"Ecommerce Order Confirmation": "email_confirmation_subject",
	"Ecommerce Sales Invoice": "email_invoice_subject",
}


def _resolve_channel_name(doc) -> str:
	"""Sales-channel for branding/sender routing. Most ecommerce docs carry
	``ecommerce_sales_channel_name`` directly; drop-ship Purchase Orders /
	Purchase Receipts / Delivery Notes do not, so fall back to the channel of
	the linked Sales Order (via the item rows' ``sales_order`` /
	``against_sales_order``). Keeps the brand sender + footer on those mails."""
	name = (doc.get("ecommerce_sales_channel_name") or "") if hasattr(doc, "get") else ""
	if name:
		return name
	for row in (doc.get("items") or []):
		so = row.get("sales_order") or row.get("against_sales_order")
		if so:
			ch = frappe.db.get_value("Sales Order", so, "ecommerce_sales_channel_name")
			if ch:
				return ch
	return ""


class ChannelAwareNotification(Notification):
	def send(self, doc):
		channel_name = _resolve_channel_name(doc)

		if not channel_name:
			return super().send(doc)

		brand = get_branding(channel_name)
		email_account = _resolve_email_account(brand, doc.doctype)
		sender_name = brand.get("sender_name") or ""
		bcc_email = brand.get("bcc_email") or ""
		language = brand.get("language") or ""
		einvoice_active = (
			bool(brand.get("enable_einvoice"))
			and doc.doctype == "Sales Invoice"
		)
		subject_field = SUBJECT_FIELD_BY_NOTIFICATION.get(self.name)
		brand_subject = (brand.get(subject_field) or "").strip() if subject_field else ""

		if not (email_account or sender_name or bcc_email or language or einvoice_active or brand_subject):
			return super().send(doc)

		original_sender = self.sender
		original_sender_email = self.sender_email
		original_bcc_rows = list(self.recipients or [])
		original_subject = self.subject
		original_lang = frappe.local.lang
		original_doc_lang = doc.get("language") if hasattr(doc, "get") else _UNSET
		original_attach_print = frappe.attach_print if einvoice_active else None

		try:
			if language:
				frappe.local.lang = language
				# attach_print picks up doc.language when rendering Print Format PDFs.
				doc.language = language
			if einvoice_active:
				frappe.attach_print = _einvoice_attach_print(original_attach_print, doc.name)
			if brand_subject:
				self.subject = brand_subject

			# Frappe builds the From header as formataddr((self.sender, self.sender_email)).
			# Override BOTH so the actual sending address comes from the branding-configured
			# account, not from defaults baked into the notification fixture.
			if email_account:
				if not frappe.db.exists("Email Account", email_account):
					frappe.log_error(
						f"Branding for channel '{channel_name}' references missing Email Account "
						f"'{email_account}' (doctype: {doc.doctype}). Falling back to system default.",
						"ChannelAwareNotification",
					)
				else:
					account_email_id = frappe.db.get_value("Email Account", email_account, "email_id")
					self.sender = sender_name or email_account
					if account_email_id:
						self.sender_email = account_email_id
			elif sender_name:
				self.sender = sender_name
			if bcc_email:
				has_bcc = any((r.get("bcc") or "") == bcc_email for r in (self.recipients or []))
				if not has_bcc:
					self.append("recipients", {"bcc": bcc_email})
			return super().send(doc)
		finally:
			self.sender = original_sender
			self.sender_email = original_sender_email
			self.recipients = original_bcc_rows
			self.subject = original_subject
			frappe.local.lang = original_lang
			if original_doc_lang is not _UNSET:
				doc.language = original_doc_lang
			if original_attach_print is not None:
				frappe.attach_print = original_attach_print

	def get_attachment(self, doc) -> list[dict]:
		"""Append the raw E-Invoice XML as a separate attachment when the
		channel has E-Invoice enabled and the document is a Sales Invoice."""
		attachments = super().get_attachment(doc) or []

		channel_name = doc.get("ecommerce_sales_channel_name") if hasattr(doc, "get") else None
		if not channel_name or doc.doctype != "Sales Invoice":
			return attachments

		brand = get_branding(channel_name)
		if not brand.get("enable_einvoice"):
			return attachments

		try:
			from eu_einvoice.european_e_invoice.custom.sales_invoice import get_einvoice
			xml_bytes = get_einvoice(doc.name)
		except ImportError:
			frappe.log_error(
				"E-Invoice attachment requested but eu_einvoice app is not installed",
				"ChannelAwareNotification E-Invoice",
			)
			return attachments
		except Exception as e:
			frappe.log_error(
				f"E-Invoice XML generation for {doc.name} failed: {e}",
				"ChannelAwareNotification E-Invoice",
			)
			return attachments

		attachments.append({
			"fname": f"{doc.name}.xml",
			"fcontent": xml_bytes,
		})
		return attachments


def _resolve_email_account(brand: dict, doctype: str) -> str:
	"""Pick the right email account for the given doctype.

	Invoice-related docs prefer the dedicated invoice account; everything else
	uses the orders account. Each side falls back to the other when only one
	is configured, so a partially-set-up branding still routes mail somewhere.
	"""
	orders_account = (brand.get("outgoing_email_account") or "").strip()
	invoice_account = (brand.get("outgoing_email_account_invoice") or "").strip()

	if doctype in INVOICE_DOCTYPES:
		return invoice_account or orders_account
	return orders_account or invoice_account


def _einvoice_attach_print(original_fn, invoice_name: str):
	"""Wrap ``frappe.attach_print`` so the Sales Invoice PDF for ``invoice_name``
	gets the ZUGFeRD XML embedded into it.

	All other ``attach_print`` calls (other invoices, other doctypes, other
	docs that just happen to share this notification instance) pass through
	unmodified.
	"""
	def wrapped(*args, **kwargs):
		result = original_fn(*args, **kwargs)
		if not isinstance(result, dict) or not result.get("fcontent"):
			return result

		doctype = kwargs.get("doctype")
		name = kwargs.get("name")
		if doctype is None and args:
			doctype = args[0]
		if name is None and len(args) > 1:
			name = args[1]

		if doctype != "Sales Invoice" or name != invoice_name:
			return result

		fname = result.get("fname") or ""
		if not fname.lower().endswith(".pdf"):
			return result

		try:
			from eu_einvoice.european_e_invoice.custom.sales_invoice import attach_xml_to_pdf
			result["fcontent"] = attach_xml_to_pdf(name, result["fcontent"])
		except ImportError:
			frappe.log_error(
				"E-Invoice embed requested but eu_einvoice is not installed",
				"ChannelAwareNotification E-Invoice",
			)
		except Exception as e:
			frappe.log_error(
				f"E-Invoice PDF embed for {name} failed: {e}",
				"ChannelAwareNotification E-Invoice",
			)
		return result

	return wrapped
