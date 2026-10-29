"""Customer order comment as a fulfillment hint (cross-channel).

Storefronts let the buyer leave a free-text comment on an order (Shopware 6:
``order.customerComment``). Those notes are mostly for the warehouse —
"please ring twice", "deliver to the side entrance", or a pickup
instruction such as ``ABHOLUNG: <event>, <city>, <date>`` written by a
storefront pickup plugin. ERPNext has no native field for this, so the
comment would otherwise be lost before picking and packing.

Design:

- ``Sales Order.ecommerce_customer_comment`` holds the comment as plain text.
  It stays editable (``allow_on_submit``) on purpose: the same hint is
  useful for manual / phone orders, and staff must be able to correct or
  add a pickup instruction after the order was submitted. The storefront
  only overwrites it when it delivers a different, non-empty comment.
- ``Delivery Note`` / ``Pick List`` / ``Packing Slip`` carry a read-only
  copy that is recomputed from the linked Sales Order(s) in ``validate``
  (i.e. on every save of a draft and on submit; never after submit). A hook (instead of relying on ``get_mapped_doc``
  copying same-named fields) also covers documents built from several
  orders, Pick Lists (linked per location row) and Delivery Notes created
  outside the standard "Make" buttons.
- ``ecommerce_comment_highlight`` is set when the comment starts with one of
  the configured prefixes (default ``ABHOLUNG:``). Forms and print formats
  use it to render the hint in red instead of yellow. Prefixes are
  configurable per site via the ``ecommerce_comment_highlight_prefixes``
  site-config key (list or comma-separated string).

Values are normalised to plain text (tags stripped, entities decoded,
control characters removed, length capped) before they are stored; every
renderer must still escape them.
"""

import html
import re
from collections.abc import Iterable

import frappe

COMMENT_FIELD = "ecommerce_customer_comment"
HIGHLIGHT_FIELD = "ecommerce_comment_highlight"

MAX_COMMENT_LENGTH = 2000
DEFAULT_HIGHLIGHT_PREFIXES = ("ABHOLUNG:",)
HIGHLIGHT_PREFIXES_CONFIG_KEY = "ecommerce_comment_highlight_prefixes"

DOWNSTREAM_DOCTYPES = ("Delivery Note", "Pick List", "Packing Slip")

_TAG_RE = re.compile(r"<[^>]*>")
_BREAK_TAG_RE = re.compile(r"<\s*(br|/p|/div|/li)\s*/?\s*>", re.IGNORECASE)
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_MANY_BLANK_LINES_RE = re.compile(r"\n{3,}")


def _comment_field(label: str, insert_after: str, *, downstream: bool) -> dict:
	field = {
		"fieldname": COMMENT_FIELD,
		"label": label,
		"fieldtype": "Small Text",
		"insert_after": insert_after,
		"no_copy": 1,
		"translatable": 0,
	}
	if downstream:
		field.update(
			{
				"read_only": 1,
				"description": "Wird beim Speichern/Buchen aus dem/den verknüpften Auftrag/Aufträgen übernommen.",
			}
		)
	else:
		field.update(
			{
				"allow_on_submit": 1,
				"description": (
					"Kommentar des Kunden aus dem Shop (oder manuell erfasst). Wird an "
					"Kommissionierliste, Lieferschein und Packliste weitergegeben."
				),
			}
		)
	return field


def _highlight_field() -> dict:
	return {
		"fieldname": HIGHLIGHT_FIELD,
		"label": "Hervorgehobener Hinweis",
		"fieldtype": "Check",
		"insert_after": COMMENT_FIELD,
		"read_only": 1,
		"allow_on_submit": 1,
		"no_copy": 1,
		"print_hide": 1,
		"in_standard_filter": 1,
		"description": "Automatisch gesetzt, wenn der Kommentar mit einem konfigurierten Präfix beginnt (Standard: ABHOLUNG:).",
	}


CUSTOMER_COMMENT_FIELDS = {
	"Sales Order": [
		_comment_field("Kundenkommentar", "customer_name", downstream=False),
		_highlight_field(),
	],
	"Delivery Note": [
		_comment_field("Kundenkommentar (aus Auftrag)", "customer_name", downstream=True),
		_highlight_field(),
	],
	"Pick List": [
		_comment_field("Kundenkommentar (aus Auftrag)", "customer", downstream=True),
		_highlight_field(),
	],
	"Packing Slip": [
		_comment_field("Kundenkommentar (aus Auftrag)", "delivery_note", downstream=True),
		_highlight_field(),
	],
}


def setup_custom_fields() -> None:
	"""Install / update the comment fields. Idempotent (``update=True``)."""
	from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

	fields = {dt: defs for dt, defs in CUSTOMER_COMMENT_FIELDS.items() if frappe.db.exists("DocType", dt)}
	if fields:
		create_custom_fields(fields, update=True)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def normalize_customer_comment(raw, max_length: int = MAX_COMMENT_LENGTH) -> str:
	"""Turn whatever the storefront sent into trimmed plain text.

	Markup is removed rather than escaped so the stored value reads the same
	in every renderer; renderers escape on output anyway. Entities are
	decoded and tags stripped a second time so double-encoded markup
	(``&lt;b&gt;``) cannot survive as live HTML.
	"""
	if raw is None:
		return ""
	if not isinstance(raw, str):
		raw = str(raw)

	text = raw.replace("\r\n", "\n").replace("\r", "\n")
	text = _BREAK_TAG_RE.sub("\n", text)
	text = _TAG_RE.sub("", text)
	text = html.unescape(text)
	text = _TAG_RE.sub("", text)
	text = text.replace("\xa0", " ").replace("\t", " ")
	text = _CONTROL_CHARS_RE.sub("", text)
	text = "\n".join(line.strip() for line in text.split("\n"))
	text = _MANY_BLANK_LINES_RE.sub("\n\n", text).strip()

	if max_length and len(text) > max_length:
		text = text[: max_length - 1].rstrip() + "…"
	return text


def _parse_prefixes(value) -> tuple[str, ...]:
	if not value:
		return ()
	if isinstance(value, str):
		value = value.split(",")
	return tuple(p.strip() for p in value if isinstance(p, str) and p.strip())


def get_highlight_prefixes() -> tuple[str, ...]:
	"""Prefixes from site config, falling back to the generic default.

	An explicitly empty list in site config disables highlighting.
	"""
	conf = frappe.conf.get(HIGHLIGHT_PREFIXES_CONFIG_KEY) if getattr(frappe, "conf", None) else None
	if conf is None:
		return DEFAULT_HIGHLIGHT_PREFIXES
	return _parse_prefixes(conf)


def is_highlighted(comment: str | None, prefixes: Iterable[str] | None = None) -> bool:
	"""True when the comment starts (case-insensitive) with one of ``prefixes``."""
	if not comment:
		return False
	if prefixes is None:
		prefixes = get_highlight_prefixes()
	head = comment.lstrip().casefold()
	return any(head.startswith(p.casefold()) for p in prefixes if p)


def combine_comments(comments_by_order: dict[str, str]) -> str:
	"""Join the comments of several orders for a Pick List / Delivery Note.

	With a single order the text is used as-is; with several, each block is
	prefixed with its order name so the picker knows which hint belongs to
	which order.
	"""
	non_empty = [(so, c) for so, c in comments_by_order.items() if c]
	if not non_empty:
		return ""
	if len(comments_by_order) == 1:
		return non_empty[0][1]
	return "\n\n".join(f"[{so}] {comment}" for so, comment in non_empty)


# ---------------------------------------------------------------------------
# Document helpers / hooks
# ---------------------------------------------------------------------------


def _has_fields(doctype: str) -> bool:
	meta = frappe.get_meta(doctype)
	return bool(meta.has_field(COMMENT_FIELD) and meta.has_field(HIGHLIGHT_FIELD))


def set_customer_comment(doc, raw) -> bool:
	"""Set the normalised comment + highlight flag on an in-memory document.

	Returns True when a non-empty comment was set. Used by channel importers
	before insert.
	"""
	if not _has_fields(doc.doctype):
		return False
	comment = normalize_customer_comment(raw)
	if not comment:
		return False
	doc.set(COMMENT_FIELD, comment)
	doc.set(HIGHLIGHT_FIELD, 1 if is_highlighted(comment) else 0)
	return True


def update_sales_order_comment(sales_order: str, raw) -> bool:
	"""Apply a storefront comment change to an existing (possibly submitted) Sales Order.

	The storefront is the source of the comment, but an empty / missing value
	never clears what staff entered — update payloads frequently omit fields.
	Returns True when the stored value changed.
	"""
	if not _has_fields("Sales Order"):
		return False
	comment = normalize_customer_comment(raw)
	if not comment:
		return False
	current = frappe.db.get_value("Sales Order", sales_order, COMMENT_FIELD)
	if (current or "") == comment:
		return False
	frappe.db.set_value(
		"Sales Order",
		sales_order,
		{COMMENT_FIELD: comment, HIGHLIGHT_FIELD: 1 if is_highlighted(comment) else 0},
	)
	return True


def sales_order_validate(doc, method=None):
	"""Normalise manual edits / checkout-field mappings and refresh the flag.

	Registered for ``validate`` and ``before_update_after_submit``.
	"""
	if not _has_fields(doc.doctype):
		return
	comment = normalize_customer_comment(doc.get(COMMENT_FIELD))
	if comment != (doc.get(COMMENT_FIELD) or ""):
		doc.set(COMMENT_FIELD, comment)
	highlight = 1 if is_highlighted(comment) else 0
	if (doc.get(HIGHLIGHT_FIELD) or 0) != highlight:
		doc.set(HIGHLIGHT_FIELD, highlight)


def _comments_from_sales_orders(so_names: Iterable[str]) -> tuple[str, int]:
	ordered = []
	for name in so_names:
		if name and name not in ordered:
			ordered.append(name)
	if not ordered or not _has_fields("Sales Order"):
		return "", 0

	rows = frappe.get_all(
		"Sales Order",
		filters={"name": ("in", ordered)},
		fields=["name", COMMENT_FIELD, HIGHLIGHT_FIELD],
	)
	by_name = {r.name: r for r in rows}
	comments = {so: (by_name[so].get(COMMENT_FIELD) or "") for so in ordered if so in by_name}
	highlight = int(any(by_name[so].get(HIGHLIGHT_FIELD) for so in ordered if so in by_name))
	return combine_comments(comments), highlight


def _apply_downstream(doc, comment: str, highlight: int) -> None:
	doc.set(COMMENT_FIELD, comment)
	doc.set(HIGHLIGHT_FIELD, highlight)


def propagate_to_delivery_note(doc, method=None):
	"""Delivery Note validate hook: copy the comment(s) of the linked orders."""
	if not _has_fields(doc.doctype):
		return
	so_names = [row.get("against_sales_order") for row in (doc.get("items") or [])]
	if not any(so_names):
		return
	_apply_downstream(doc, *_comments_from_sales_orders(so_names))


def propagate_to_pick_list(doc, method=None):
	"""Pick List validate hook: comments of every order referenced by a location row."""
	if not _has_fields(doc.doctype):
		return
	so_names = [row.get("sales_order") for row in (doc.get("locations") or [])]
	if not any(so_names):
		return
	_apply_downstream(doc, *_comments_from_sales_orders(so_names))


def propagate_to_packing_slip(doc, method=None):
	"""Packing Slip validate hook: take the value of its Delivery Note."""
	if not doc.get("delivery_note") or not _has_fields(doc.doctype):
		return
	if not _has_fields("Delivery Note"):
		return
	row = frappe.db.get_value(
		"Delivery Note", doc.delivery_note, [COMMENT_FIELD, HIGHLIGHT_FIELD], as_dict=True
	)
	if row is None:
		return
	_apply_downstream(doc, row.get(COMMENT_FIELD) or "", int(row.get(HIGHLIGHT_FIELD) or 0))
