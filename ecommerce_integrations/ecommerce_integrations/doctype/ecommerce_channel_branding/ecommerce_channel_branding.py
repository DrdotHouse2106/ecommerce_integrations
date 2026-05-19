"""Ecommerce Channel Branding DocType — per-channel branding, texts, email config."""

import frappe
from frappe.model.document import Document


BRANDING_FIELDS = [
	"display_name", "shop_url", "support_email",
	"primary_color", "dark_color", "logo", "logo_data_uri",
	"outgoing_email_account", "outgoing_email_account_invoice",
	"sender_name", "language", "reply_to_email", "bcc_email",
	"enable_einvoice",
	"order_ack_title", "order_ack_intro",
	"order_confirmation_title", "order_confirmation_intro",
	"delivery_note_title", "delivery_note_intro",
	"email_acknowledgment_subject", "email_acknowledgment_greeting", "email_acknowledgment_body",
	"email_confirmation_subject", "email_confirmation_greeting", "email_confirmation_body",
	"email_invoice_subject", "email_invoice_greeting", "email_invoice_body",
	"signature_html", "imprint_html", "privacy_policy_url", "terms_url",
]

DEFAULTS = {
	"display_name": "",
	"shop_url": "",
	"support_email": "",
	"primary_color": "#333333",
	"dark_color": "#000000",
	"logo": "",
	"logo_data_uri": "",
	"outgoing_email_account": "",
	"outgoing_email_account_invoice": "",
	"sender_name": "",
	"language": "de",
	"reply_to_email": "",
	"bcc_email": "",
	"enable_einvoice": 0,
	# Defaults are English source strings. They render in German by default
	# because channel branding falls back to language="de". They are translated
	# to the channel language at render time — either via the consuming template (which calls
	# _() on title fields) or via embedded {{ _("...") }} Jinja in body fields.
	# See translations/de.csv for German renderings.
	"order_ack_title": "Order Acknowledgment",
	"order_ack_intro": '{{ _("We have received your order and are reviewing it:") }}',
	"order_confirmation_title": "Order Confirmation",
	"order_confirmation_intro": '{{ _("We hereby confirm your order:") }}',
	"delivery_note_title": "Delivery Note",
	"delivery_note_intro": '{{ _("Enclosed please find your delivery:") }}',
	"email_acknowledgment_subject": '{{ _("Order Acknowledgment") }} {{ doc.name }}',
	"email_acknowledgment_greeting": "{{ auto_greeting }},",
	"email_acknowledgment_body": '<p>{{ _("thank you for your order. We have received your request and will process it as quickly as possible.") }}</p><p>{{ _("Below you will find an overview of the items you ordered.") }}</p>',
	"email_confirmation_subject": '{{ _("Order Confirmation") }} {{ doc.name }}',
	"email_confirmation_greeting": "{{ auto_greeting }},",
	"email_confirmation_body": '<p>{{ _("we hereby confirm your order as binding. Please find the details in the attached order confirmation.") }}</p>',
	"email_invoice_subject": '{{ _("Your invoice") }} {{ doc.name }}',
	"email_invoice_greeting": "{{ auto_greeting }},",
	"email_invoice_body": '<p>{{ _("please find your invoice attached as a PDF. If enabled, an electronic invoice (XRechnung/ZUGFeRD) is included both as a separate XML file and embedded into the PDF.") }}</p><p>{{ _("Please find the payment details on the invoice.") }}</p>',
	"signature_html": "",
	"imprint_html": "",
	"privacy_policy_url": "",
	"terms_url": "",
}


class EcommerceChannelBranding(Document):
	pass


@frappe.whitelist()
def get_branding(channel_name: str = "") -> dict:
	"""Return branding config for a channel, with safe fallback to defaults.

	Called from Jinja in Print Formats. Cached per-request.
	"""
	cache_key = f"_ecommerce_branding_{channel_name or '_default_'}"
	if hasattr(frappe.local, cache_key):
		return getattr(frappe.local, cache_key)

	result = dict(DEFAULTS)
	result["display_name"] = channel_name or "Shop"

	if channel_name:
		values = frappe.db.get_value(
			"Ecommerce Channel Branding", channel_name, BRANDING_FIELDS, as_dict=True
		)
		if values:
			for k, v in values.items():
				if v:
					result[k] = v
			# Logo preference: data URI > uploaded file
			result["logo"] = values.get("logo_data_uri") or values.get("logo") or ""
			# Dark color falls back to primary
			if not result["dark_color"] or result["dark_color"] == "#000000":
				result["dark_color"] = result["primary_color"]

	setattr(frappe.local, cache_key, result)
	return result


def get_branding_for_doc(doc) -> dict:
	"""Resolve branding from a Sales Order / Delivery Note / Sales Invoice."""
	return get_branding(doc.get("ecommerce_sales_channel_name") or "")


@frappe.whitelist()
def build_greeting_context(doc, lang: str = "") -> dict:
	"""Produce a placeholder context for greeting/body Jinja rendering.

	Pulls first_name, last_name, gender, salutation from the doc's Contact
	(``contact_person``), with fallback parsing of ``customer_name``.
	Computes ``auto_greeting`` as a localized full sentence so users can write
	``{{ auto_greeting }}`` in the branding fields and get a correct greeting
	without writing any logic themselves.
	"""
	if not doc:
		return _empty_greeting_context()

	first_name = last_name = gender = salutation = ""

	contact_name = doc.get("contact_person") if hasattr(doc, "get") else None
	if contact_name and frappe.db.exists("Contact", contact_name):
		contact = frappe.db.get_value(
			"Contact", contact_name,
			["first_name", "last_name", "gender", "salutation"],
			as_dict=True,
		)
		if contact:
			first_name = contact.first_name or ""
			last_name = contact.last_name or ""
			gender = contact.gender or ""
			salutation = contact.salutation or ""

	customer_name = (doc.get("customer_name") or "") if hasattr(doc, "get") else ""
	if not (first_name or last_name) and customer_name and " " in customer_name:
		first_name, last_name = customer_name.split(" ", 1)

	target_lang = (lang or frappe.local.lang or "").split("-")[0].lower()
	auto_greeting = _auto_greeting(target_lang, salutation, last_name, gender)

	return {
		"first_name": first_name,
		"last_name": last_name,
		"customer_name": customer_name,
		"gender": gender,
		"salutation": salutation,
		"auto_greeting": auto_greeting,
	}


def _empty_greeting_context() -> dict:
	return {
		"first_name": "",
		"last_name": "",
		"customer_name": "",
		"gender": "",
		"salutation": "",
		"auto_greeting": _auto_greeting(frappe.local.lang or "", "", "", ""),
	}


def _auto_greeting(lang: str, salutation: str, last_name: str, gender: str) -> str:
	"""Localized greeting line. Falls back to gender-neutral salutation."""
	sal = (salutation or "").lower().rstrip(".")
	lang = (lang or "").split("-")[0].lower()

	if lang == "de":
		if last_name and (sal in ("mr", "herr") or gender == "Male"):
			return f"Sehr geehrter Herr {last_name}"
		if last_name and (sal in ("ms", "mrs", "frau") or gender == "Female"):
			return f"Sehr geehrte Frau {last_name}"
		return "Sehr geehrte Damen und Herren"

	# English / default
	if last_name and (sal == "mr" or gender == "Male"):
		return f"Dear Mr. {last_name}"
	if last_name and (sal in ("ms", "mrs") or gender == "Female"):
		return f"Dear Ms. {last_name}"
	return "Dear Sir or Madam"


@frappe.whitelist()
def get_invoice_payment_context(doc) -> dict:
	"""Resolve payment_mode + payment_status for a Sales Invoice/Sales Order.

	For Sales Invoice we walk the linked Sales Order(s) via SI Item.sales_order
	to read shopware-style payment fields. The result drives the payment box
	rendering (paid badge / prepayment instructions / invoice payment info).
	"""
	online_modes = (
		"PayPal", "Kreditkarte", "Stripe", "Klarna", "Sofort",
		"Apple Pay", "Google Pay", "SEPA Lastschrift", "PayPal Rechnungskauf",
	)
	invoice_modes = ("Auf Rechnung", "Invoice", "Kauf auf Rechnung", "Purchase on Account")

	def _empty():
		return {
			"payment_mode": "",
			"payment_status": "",
			"is_paid": False,
			"is_invoice": False,
			"is_prepayment": False,
		}

	if not doc:
		return _empty()

	payment_mode = ""
	payment_status = ""

	if doc.doctype == "Sales Order":
		payment_mode = doc.get("shopware_erpnext_mode_of_payment") or ""
		payment_status = doc.get("shopware_payment_status") or ""

	elif doc.doctype == "Sales Invoice":
		so_names = {item.get("sales_order") for item in (doc.items or []) if item.get("sales_order")}
		for so_name in so_names:
			if not so_name:
				continue
			row = frappe.db.get_value(
				"Sales Order", so_name,
				["shopware_erpnext_mode_of_payment", "shopware_payment_status"],
				as_dict=True,
			)
			if row and (row.shopware_erpnext_mode_of_payment or row.shopware_payment_status):
				payment_mode = row.shopware_erpnext_mode_of_payment or ""
				payment_status = row.shopware_payment_status or ""
				break

	is_paid = payment_mode in online_modes or payment_status == "Paid"
	is_invoice = payment_mode in invoice_modes
	is_prepayment = payment_mode == "Vorkasse"

	return {
		"payment_mode": payment_mode,
		"payment_status": payment_status,
		"is_paid": is_paid,
		"is_invoice": is_invoice,
		"is_prepayment": is_prepayment,
	}


@frappe.whitelist()
def get_default_bank_info(company: str) -> dict:
	"""First default Bank Account for the company, with IBAN + BIC."""
	if not company:
		return {"iban": "", "bic": "", "bank": ""}
	rows = frappe.get_all(
		"Bank Account",
		filters={"company": company, "is_company_account": 1},
		fields=["iban", "bank", "branch_code", "is_default"],
		order_by="is_default desc",
		limit=1,
	)
	if not rows:
		return {"iban": "", "bic": "", "bank": ""}
	bank = rows[0]
	bic = ""
	if bank.bank:
		bic = frappe.db.get_value("Bank", bank.bank, "swift_number") or bank.branch_code or ""
	else:
		bic = bank.branch_code or ""
	return {"iban": bank.iban or "", "bic": bic, "bank": bank.bank or ""}


def render_branding_text(template: str, doc) -> str:
	"""Render a branding-field string (greeting/body) with Jinja placeholders."""
	if not template:
		return ""
	if "{{" not in template and "{%" not in template:
		# Plain text — short-circuit to avoid an unnecessary Jinja round-trip
		# and any sandbox surprises.
		return template
	ctx = build_greeting_context(doc)
	return frappe.render_template(template, ctx)


@frappe.whitelist()
def get_available_channels() -> list:
	"""Return distinct channel names from Medusa/Shopware sales channels + Sales Orders.

	Used by the Ecommerce Channel Branding form to populate the channel_name
	autocomplete with real channels the user actually uses.
	"""
	names = set()

	for doctype in ("Medusa Sales Channel", "Shopware Sales Channel"):
		if not frappe.db.exists("DocType", doctype):
			continue
		channels = frappe.get_all(doctype, fields=["sales_channel_name"], distinct=True)
		for c in channels:
			name = (c.get("sales_channel_name") or "").strip()
			if name:
				names.add(name)

	# Distinct values on Sales Orders
	rows = frappe.db.sql(
		"""
		SELECT DISTINCT ecommerce_sales_channel_name FROM `tabSales Order`
		WHERE ecommerce_sales_channel_name IS NOT NULL AND ecommerce_sales_channel_name != ''
		""",
		as_dict=False,
	)
	for (name,) in rows:
		if name:
			names.add(name)

	return sorted(names)


@frappe.whitelist()
def find_sample_doc(channel_name: str, doc_type: str = "Sales Order") -> str:
	"""Find a recent doc with the given channel, falling back to any recent doc."""
	if doc_type == "Delivery Note":
		result = frappe.db.sql(
			"""
			SELECT DISTINCT dn.name FROM `tabDelivery Note` dn
			JOIN `tabDelivery Note Item` dni ON dni.parent = dn.name
			JOIN `tabSales Order` so ON so.name = dni.against_sales_order
			WHERE so.ecommerce_sales_channel_name = %s
			ORDER BY dn.creation DESC LIMIT 1
			""",
			(channel_name,),
		)
		if result:
			return result[0][0]
	else:
		docs = frappe.get_all(
			doc_type,
			filters={"ecommerce_sales_channel_name": channel_name},
			order_by="creation desc",
			limit=1,
		)
		if docs:
			return docs[0].name

	# Fallback: any recent doc of this type
	any_doc = frappe.get_all(doc_type, order_by="creation desc", limit=1)
	return any_doc[0].name if any_doc else ""


class _PreviewDoc:
	"""Lightweight doc stand-in for previewing Print Formats without a real document.

	Uses instance attributes (not dict storage) to avoid collision with dict.items().
	"""

	def __init__(self, data: dict):
		self.__dict__.update(data)

	def get(self, key, default=None):
		return self.__dict__.get(key, default)

	def get_formatted(self, field):
		val = self.__dict__.get(field)
		if val is None:
			return ""
		if isinstance(val, (int, float)):
			return frappe.format_value(val, {"fieldtype": "Currency", "options": "EUR"})
		return str(val)

	def as_dict(self):
		return dict(self.__dict__)


def _build_dummy_doc(channel_name: str, doc_type: str):
	"""Build an in-memory sample doc when no real doc exists yet."""
	company = frappe.defaults.get_global_default("company") or (
		frappe.get_all("Company", limit=1, pluck="name") or ["Demo Company"]
	)
	company = company if isinstance(company, str) else company[0]

	items_data = [
		{
			"item_code": "DEMO-001",
			"item_name": "Demo Artikel A",
			"description": "Beispielartikel für Vorschau",
			"qty": 2,
			"uom": "Nos",
			"rate": 49.99,
			"net_rate": 42.01,
			"amount": 99.98,
			"net_amount": 84.02,
			"discount_total": 0,
		},
		{
			"item_code": "DEMO-002",
			"item_name": "Demo Artikel B",
			"description": "Zweiter Beispielartikel",
			"qty": 1,
			"uom": "Nos",
			"rate": 19.90,
			"net_rate": 16.72,
			"amount": 19.90,
			"net_amount": 16.72,
			"discount_total": 0,
		},
	]

	base = {
		"doctype": doc_type,
		"name": f"PREVIEW-{doc_type.replace(' ', '-')}",
		"customer": "Demo Kunde",
		"customer_name": "Demo Kunde GmbH",
		"customer_address": None,
		"address_display": "Demo Kunde GmbH\nMusterstraße 1\n10115 Berlin\nDeutschland",
		"shipping_address_name": None,
		"company": company,
		"currency": "EUR",
		"transaction_date": frappe.utils.today(),
		"posting_date": frappe.utils.today(),
		"delivery_date": frappe.utils.add_days(frappe.utils.today(), 7),
		"po_no": "PO-DEMO-1234",
		"contact_email": "kunde@example.com",
		"tax_id": "DE123456789",
		"items": [_PreviewDoc(i) for i in items_data],
		"taxes": [],
		"total_taxes_and_charges": 0,
		"net_total": 100.74,
		"grand_total": 119.88,
		"ecommerce_sales_channel_name": channel_name,
		"terms": None,
		"tax_exemption_reason": None,
		"address_display": "Demo Kunde GmbH\nMusterstraße 1\n10115 Berlin\nDeutschland",
	}
	return _PreviewDoc(base)


def _resolve_preview_doc(channel_name: str, doc_type: str):
	"""Return a (doc, is_dummy) tuple — real doc if available, else dummy."""
	sample_name = find_sample_doc(channel_name, doc_type)
	if sample_name:
		doc = frappe.get_doc(doc_type, sample_name)
		# Force the channel so the preview reflects THIS branding (not the doc's actual channel)
		doc.ecommerce_sales_channel_name = channel_name
		return doc, False
	return _build_dummy_doc(channel_name, doc_type), True


@frappe.whitelist()
def render_email_preview(channel_name: str, kind: str = "acknowledgment") -> str:
	"""Render the email body for a sample doc with the given channel's branding."""
	doc, _ = _resolve_preview_doc(channel_name, "Sales Order")
	brand = get_branding(channel_name)
	template = """{%- from "ecommerce_integrations/templates/includes/email_branding.html" import render_email with context -%}
{{ render_email(doc, brand, kind) }}"""
	return frappe.render_template(template, {"doc": doc, "brand": brand, "kind": kind})


@frappe.whitelist()
def render_pdf_preview(channel_name: str, print_format: str, doc_type: str = "Sales Order") -> str:
	"""Render the Print Format HTML for a sample doc with the given channel's branding.

	Uses frappe.get_print for real docs, falls back to direct Jinja rendering for dummies.
	"""
	doc, is_dummy = _resolve_preview_doc(channel_name, doc_type)
	if not is_dummy:
		return frappe.get_print(
			doctype=doc_type, name=doc.name, print_format=print_format, no_letterhead=False
		)

	pf = frappe.get_doc("Print Format", print_format)
	brand = get_branding(channel_name)
	return frappe.render_template(pf.html, {"doc": doc, "brand": brand, "_": frappe._})

