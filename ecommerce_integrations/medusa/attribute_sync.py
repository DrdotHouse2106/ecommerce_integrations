"""Attribute, category and transliteration helpers for Medusa product sync.

These helpers used to live in ``medusa/product_export.py`` alongside the
1900-line MedusaProductExporter class. They're pure functions that talk
to the Medusa Attributes plugin and Product Categories endpoints; lifting
them out keeps the exporter focused on the build-and-upload flow and
makes the attribute logic individually testable.

Caching: ``_get_category_map`` and ``_get_or_build_attribute_map`` both
cache their results in ``frappe.cache`` for 5 minutes. The
``_ensure_attributes_exist`` batch-create call invalidates the attribute
cache when the response reports merged values, since the merged-payload
shape doesn't carry the new value IDs needed for subsequent lookups.
"""

import re

import frappe

from ecommerce_integrations.medusa.connection import (
	medusa_request,
	medusa_request_all,
	optional_session,
)


_UMLAUT_MAP = str.maketrans({
	"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss",
	"Ä": "ae", "Ö": "oe", "Ü": "ue",
	"é": "e", "è": "e", "ê": "e", "à": "a", "â": "a",
	"ô": "o", "î": "i", "ç": "c", "ñ": "n",
})


def transliterate(text: str) -> str:
	"""Replace umlauts and accented chars with ASCII equivalents."""
	return text.translate(_UMLAUT_MAP)


def rank_for_value(v: str) -> int:
	"""Derive a stable, order-preserving rank for an attribute value.

	Numeric strings use their numeric value ("150" → 150).
	Non-numeric strings derive rank from first 4 character codes,
	preserving alphabetical order ("A" < "AA" < "B" < "Blau").
	"""
	try:
		return int(float(v))
	except (ValueError, TypeError):
		pass
	if v:
		# Pad to 4 chars so single-char values sort correctly vs multi-char
		# e.g. "B" (padded "B\0\0\0") > "AA" (padded "AA\0\0") — correct
		chars = v[:4].lower().ljust(4, '\0')
		rank = 0
		for ch in chars:
			rank = rank * 256 + ord(ch)
		return rank
	return 0


def resolve_attribute_values(entries: list, attr_map: dict) -> list:
	"""Resolve (attr_name, value) entries to ``[{attribute_id, value}]`` using ``attr_map``."""
	result = []
	for prop_name, prop_value in entries:
		attr_entry = attr_map.get(prop_name)
		if attr_entry and attr_entry.get("id"):
			result.append({"attribute_id": attr_entry["id"], "value": prop_value})
	return result


def get_category_map(session=None, base_url=None) -> dict:
	"""Get ``{item_group_name: medusa_category_id}`` map, cached for 5 minutes."""
	from ecommerce_integrations.medusa.constants import API_CATEGORIES

	cache_key = "medusa_category_map"
	cat_map = frappe.cache.get_value(cache_key)
	if cat_map is not None:
		return cat_map

	with optional_session(session, base_url) as (s, url):
		try:
			categories = medusa_request_all(s, url, API_CATEGORIES, "product_categories")
			cat_map = {c["name"]: c["id"] for c in categories}
			frappe.cache.set_value(cache_key, cat_map, expires_in_sec=300)
		except Exception:
			cat_map = {}
	return cat_map


def get_or_build_attribute_map(session=None, base_url=None) -> dict:
	"""Get ``{attr_name: {"id": attr_id, "values": {val: pv_id}}}`` map, cached."""
	cache_key = "medusa_attribute_map"
	attr_map = frappe.cache.get_value(cache_key)
	if attr_map is not None:
		return attr_map

	with optional_session(session, base_url) as (s, url):
		try:
			attrs = medusa_request_all(s, url, "/admin/plugin/attributes", "attributes")
			attr_map = {}
			for a in attrs:
				attr_map[a["name"]] = {
					"id": a["id"],
					"values": {pv["value"]: pv["id"] for pv in a.get("possible_values", [])},
				}
			frappe.cache.set_value(cache_key, attr_map, expires_in_sec=300)
		except Exception:
			attr_map = {}
	return attr_map


def ensure_attributes_exist(entries: list, session=None, base_url=None) -> dict:
	"""Ensure all attributes and their possible values exist in Medusa.

	Uses the batch endpoint POST /admin/plugin/attributes/batch to create
	missing attributes with all their values in a single request.

	Args:
		entries: list of (attr_name, value) tuples
		session: optional existing requests.Session to reuse
		base_url: optional base URL (required if session is provided)

	Returns:
		attr_map: ``{attr_name: {"id": attr_id, "values": {val: pv_id}}}``
	"""
	attr_map = get_or_build_attribute_map(session=session, base_url=base_url)

	# Collect attributes with missing values
	to_create = {}  # {name: set(values)}
	for name, value in entries:
		existing = attr_map.get(name)
		if not existing:
			to_create.setdefault(name, set()).add(value)
		elif value not in existing.get("values", {}):
			to_create.setdefault(name, set()).add(value)

	if not to_create:
		return attr_map

	# Send everything as "create" — the endpoint auto-merges new values
	# into existing attributes (no separate update path needed)
	create_payloads = []
	for name, values in to_create.items():
		handle = transliterate(name.lower())
		handle = re.sub(r'[^a-z0-9-]', '-', handle)
		handle = re.sub(r'-+', '-', handle).strip('-')
		# Derive a stable rank for each value so Medusa sorts them correctly:
		# - Numeric values ("150", "5.00") → use the numeric value as rank
		# - Non-numeric values ("A", "Blau") → derive from character codes to
		#   preserve alphabetical order across sync batches
		possible_values = []
		for v in sorted(values):
			possible_values.append({"value": v, "rank": rank_for_value(v)})
		create_payloads.append({
			"name": name,
			"handle": handle,
			"is_filterable": True,
			"is_variant_defining": False,
			"ui_component": "select",
			"possible_values": possible_values,
		})

	with optional_session(session, base_url) as (s, url):
		try:
			result = medusa_request(
				s, url, "POST",
				"/admin/plugin/attributes/batch",
				json={"create": create_payloads},
			)

			# Update cache from created attributes (full possible_values in response)
			for attr in result.get("created", []):
				attr_map[attr["name"]] = {
					"id": attr["id"],
					"values": {pv["value"]: pv["id"] for pv in attr.get("possible_values", [])},
				}

			# For merged attributes, invalidate cache entry so it's re-fetched
			# (merged response only has new_values strings, not full possible_values with IDs)
			if result.get("merged"):
				frappe.cache.delete_value("medusa_attribute_map")
				attr_map = get_or_build_attribute_map(session=s, base_url=url)
			else:
				# Mark sent values as present to prevent re-sending within same sync
				for name, values in to_create.items():
					entry = attr_map.get(name)
					if entry:
						for v in values:
							entry["values"].setdefault(v, "pending")
				frappe.cache.set_value("medusa_attribute_map", attr_map, expires_in_sec=300)
		except Exception as e:
			frappe.log_error("Medusa Attributes", f"Batch attribute sync failed: {e}")

	return attr_map
