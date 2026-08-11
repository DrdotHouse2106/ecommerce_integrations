"""Unified customer numbering — ERPNext is authoritative going forward.

Historically, Shopware's own ``customerNumber`` was written straight into
ERPNext's ``Customer.name`` on creation (see ``core.py``'s
``ShopwareCustomer.sync_customer``). New customers now go the other way:
ERPNext allocates a plain, unprefixed number (continuing the existing
WeClapp/Shopware numeric style, e.g. "15040") and pushes it out to Shopware
(``vat.py::_sync_customer_number_to_shopware``, ``push.py``).

The counter is a dedicated ``tabSeries`` row, incremented under
``SELECT ... FOR UPDATE`` — the same InnoDB row-lock primitive Frappe's own
naming series relies on, so it is safe under concurrent Customer inserts
without needing a new doctype. It must be seeded once, by the one-time
migration (``import_handlers/customer_number_migration.py``), from the
combined max of both systems' existing numbers — a bare counter with no
memory of pre-existing numbers on either side would eventually allocate a
number that collides with an old, still-live record.
"""

import frappe
from frappe import _
from frappe.utils import cint

CUSTOMER_NUMBER_SERIES_KEY = "shopware_customer_number"


def is_counter_initialized() -> bool:
	# Raw SQL rather than frappe.db.exists("Series", ...): ``Series`` is
	# Frappe's internal naming-counter table, not a fully meta-backed
	# doctype — frappe.model.naming's own getseries() always goes
	# straight to raw SQL against tabSeries for the same reason.
	row = frappe.db.sql(
		"SELECT `name` FROM `tabSeries` WHERE `name` = %s", (CUSTOMER_NUMBER_SERIES_KEY,)
	)
	return bool(row)


def seed_customer_number_counter(starting_value: int) -> None:
	"""One-time: create the counter row. Called only by the migration —
	this module has no way to look up Shopware's existing numbers itself,
	so it never guesses a safe seed on its own."""
	if is_counter_initialized():
		frappe.throw(_("Der Kundennummern-Zähler wurde bereits initialisiert."))
	frappe.db.sql(
		"INSERT INTO `tabSeries` (`name`, `current`) VALUES (%s, %s)",
		(CUSTOMER_NUMBER_SERIES_KEY, cint(starting_value)),
	)


def allocate_next_customer_number() -> str:
	row = frappe.db.sql(
		"SELECT `current` FROM `tabSeries` WHERE `name` = %s FOR UPDATE",
		(CUSTOMER_NUMBER_SERIES_KEY,),
	)
	if not row:
		frappe.throw(
			_(
				"Der Kundennummern-Zähler wurde noch nicht initialisiert. Bitte zuerst die "
				"einmalige Migration ausführen (Shopware-Einstellungen → "
				"\"Kundennummern-Zähler initialisieren & bestehende Kunden abgleichen\")."
			)
		)
	next_number = cint(row[0][0]) + 1
	frappe.db.sql(
		"UPDATE `tabSeries` SET `current` = %s WHERE `name` = %s",
		(next_number, CUSTOMER_NUMBER_SERIES_KEY),
	)
	return str(next_number)


def autoname_customer_for_shopware_push(doc, method=None):
	"""``Customer.autoname`` doc_event.

	Every inbound-sync creation path (Shopware/Shopify/Medusa/Unicommerce)
	creates the Customer via ``insert(set_name=...)``, which bypasses
	autoname doc_events entirely — so this only ever fires for a
	brand-new, ERPNext-originated Customer, and only overrides naming when
	the operator ticked "Nach Shopware übertragen" at creation time.
	"""
	if doc.flags.from_integration:
		return
	if not getattr(doc, "push_to_shopware", False):
		return
	doc.name = allocate_next_customer_number()
