from time import perf_counter

import frappe
from frappe import _
from frappe.utils.nestedset import get_root_of


def resolve_customer_type(is_business: bool) -> str:
    """Map a business/individual flag to a valid ``Customer.customer_type``
    option on *this* site.

    Sites sometimes customize this Select field's raw option values (e.g.
    German "Firma" / "Einzelperson" / "GbR" instead of ERPNext's default
    "Company" / "Individual" / "Partnership"). Hardcoding the English
    default then fails Select validation with a confusing error where the
    rejected value and a valid option can even render identically once
    translated. Resolving dynamically against the doctype's actual current
    options avoids that regardless of how the site customized it.
    """
    options = (frappe.get_meta("Customer").get_field("customer_type").options or "").splitlines()
    options = [o.strip() for o in options if o.strip()]

    business_hints = ("company", "firma", "business", "unternehmen", "gmbh")
    individual_hints = ("individual", "einzelperson", "person", "privat")
    hints = business_hints if is_business else individual_hints

    for option in options:
        if option.lower() in hints:
            return option
    for option in options:
        if any(hint in option.lower() for hint in hints):
            return option

    # Nothing matched (unlikely) — fall back to the ERPNext default.
    return "Company" if is_business else "Individual"


class EcommerceCustomer:
	def __init__(self, customer_id: str, customer_id_field: str, integration: str):
		self.customer_id = customer_id
		self.customer_id_field = customer_id_field
		self.integration = integration

	def is_synced(self) -> bool:
		"""Check if customer on Ecommerce site is synced with ERPNext"""

		return bool(frappe.db.exists("Customer", {self.customer_id_field: self.customer_id}))

	def get_customer_doc(self):
		"""Get ERPNext customer document."""
		if self.is_synced():
			return frappe.get_last_doc("Customer", {self.customer_id_field: self.customer_id})
		else:
			raise frappe.DoesNotExistError()

	def sync_customer(self, customer_name: str, customer_group: str) -> None:
		"""Create customer in ERPNext if one does not exist already.

		Uses the ecommerce customer_id as the ERPNext document name (ID column)
		to ensure uniqueness and prevent duplicate customer errors.
		"""
		# Check if customer with this ID already exists
		if frappe.db.exists("Customer", self.customer_id):
			# Customer exists - just update the customer_id_field if needed
			frappe.db.set_value("Customer", self.customer_id, self.customer_id_field, self.customer_id)
			return

		customer = frappe.get_doc(
			{
				"doctype": "Customer",
				self.customer_id_field: self.customer_id,
				"customer_name": customer_name,
				"customer_group": customer_group,
				"territory": get_root_of("Territory"),
				"customer_type": resolve_customer_type(is_business=False),
			}
		)

		customer.flags.ignore_mandatory = True
		# Force the document name to be the customer_id (bypasses autoname)
		# Use try/except to handle race conditions where multiple webhooks
		# try to create the same customer simultaneously
		try:
			customer.insert(ignore_permissions=True, set_name=self.customer_id)
		except frappe.DuplicateEntryError:
			# Customer was created by another concurrent process - that's fine
			frappe.db.set_value("Customer", self.customer_id, self.customer_id_field, self.customer_id)
			return

	def get_customer_address_doc(self, address_type: str):
		"""Get customer address by type using Dynamic Links.

		Addresses are linked to customers via the Dynamic Link child table,
		not a direct link_name field. This method uses the correct query.
		"""
		customer = self.get_customer_doc().name
		addresses = frappe.db.sql(
			"""
				SELECT a.name
				FROM `tabAddress` a
				INNER JOIN `tabDynamic Link` dl ON dl.parent = a.name AND dl.parenttype = 'Address'
				WHERE dl.link_doctype = 'Customer'
				AND dl.link_name = %s
				AND a.address_type = %s
				ORDER BY a.creation DESC
				LIMIT 1
			""",
			(customer, address_type),
			as_dict=True,
		)
		if addresses:
			return frappe.get_doc("Address", addresses[0].name)
		return None

	def create_customer_address(self, address: dict[str, str]) -> None:
		"""Create address from dictionary containing fields used in Address doctype of ERPNext."""

		customer_doc = self.get_customer_doc()
		logger = frappe.logger(self.integration or "ecommerce_integrations")
		log = logger.info if self.integration == "shopware6" else logger.debug
		address_type = address.get("address_type", "Unknown")
		address_id = address.get("shopware_address_id") or address.get("shopify_address_id") or ""

		doc = frappe.get_doc(
			{
				"doctype": "Address",
				**address,
				"links": [{"link_doctype": "Customer", "link_name": customer_doc.name}],
			}
		)

		log(
			f"Address.insert start: integration={self.integration}, customer={customer_doc.name}, "
			f"type={address_type}, address_id={address_id or 'n/a'}"
		)
		started_at = perf_counter()
		doc.insert(ignore_mandatory=True, ignore_permissions=True)
		log(
			f"Address.insert done in {perf_counter() - started_at:.2f}s: integration={self.integration}, "
			f"customer={customer_doc.name}, type={address_type}, address_id={address_id or 'n/a'}"
		)

	def create_customer_contact(self, contact: dict[str, str]) -> None:
		"""Create contact from dictionary containing fields used in Address doctype of ERPNext."""

		customer_doc = self.get_customer_doc()
		logger = frappe.logger(self.integration or "ecommerce_integrations")
		log = logger.info if self.integration == "shopware6" else logger.debug
		email = contact.get("email_id")
		if not email and contact.get("email_ids"):
			email = contact.get("email_ids")[0].get("email_id")

		doc = frappe.get_doc(
			{
				"doctype": "Contact",
				**contact,
				"links": [{"link_doctype": "Customer", "link_name": customer_doc.name}],
			}
		)

		log(
			f"Contact.insert start: integration={self.integration}, customer={customer_doc.name}, "
			f"email={email or 'n/a'}"
		)
		started_at = perf_counter()
		doc.insert(ignore_mandatory=True, ignore_permissions=True)
		log(
			f"Contact.insert done in {perf_counter() - started_at:.2f}s: integration={self.integration}, "
			f"customer={customer_doc.name}, email={email or 'n/a'}"
		)
