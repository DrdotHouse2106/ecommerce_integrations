"""Unit tests for medusa/attribute_sync.py pure helpers.

These exercise the transliteration and rank-derivation logic without
touching the Medusa API or the database. The cached-map helpers
(``get_category_map`` / ``get_or_build_attribute_map`` /
``ensure_attributes_exist``) are covered separately under the
IntegrationTestCase suite once an installed Medusa Setting is available;
they're skipped here so this module runs in any checkout.
"""

import unittest

from ecommerce_integrations.medusa.attribute_sync import (
	rank_for_value,
	resolve_attribute_values,
	transliterate,
)


class TestTransliterate(unittest.TestCase):
	def test_keeps_ascii_untouched(self):
		self.assertEqual(transliterate("Hello"), "Hello")

	def test_replaces_german_umlauts(self):
		self.assertEqual(transliterate("Müller"), "Mueller")
		self.assertEqual(transliterate("Größe"), "Groesse")
		self.assertEqual(transliterate("Ärger"), "aerger")

	def test_replaces_accented_chars(self):
		# Only the chars listed in ``_UMLAUT_MAP`` are transliterated; the map
		# intentionally omits less common diacritics (ï, ã, ë, etc.) since the
		# fork ships for DE-locale shops and the map is curated to that set.
		self.assertEqual(transliterate("café"), "cafe")
		self.assertEqual(transliterate("crème"), "creme")
		self.assertEqual(transliterate("garçon"), "garcon")
		self.assertEqual(transliterate("être"), "etre")

	def test_handles_empty(self):
		self.assertEqual(transliterate(""), "")


class TestRankForValue(unittest.TestCase):
	def test_numeric_strings_use_value(self):
		self.assertEqual(rank_for_value("150"), 150)
		self.assertEqual(rank_for_value("0"), 0)
		self.assertEqual(rank_for_value("-5"), -5)

	def test_decimal_strings_truncate(self):
		self.assertEqual(rank_for_value("5.99"), 5)
		self.assertEqual(rank_for_value("12.5"), 12)

	def test_non_numeric_preserves_alpha_order(self):
		# Alphabetical: A < B < ... < Z; longer prefixes break ties
		ranks = [rank_for_value(v) for v in ("A", "Aa", "B", "Blau", "Zz")]
		self.assertEqual(ranks, sorted(ranks))

	def test_short_value_sorts_before_extension(self):
		# "B" must come before "BB" in the rank ordering — the ljust('\0') keeps
		# this stable. Regression: the previous impl used spaces which would
		# break the ordering for case-mixed inputs.
		self.assertLess(rank_for_value("B"), rank_for_value("BB"))

	def test_empty_string_is_zero(self):
		self.assertEqual(rank_for_value(""), 0)


class TestResolveAttributeValues(unittest.TestCase):
	def test_pairs_against_attr_map(self):
		attr_map = {
			"Color": {"id": "attr-color", "values": {"Red": "pv-red"}},
			"Size":  {"id": "attr-size",  "values": {"L":   "pv-l"}},
		}
		entries = [("Color", "Red"), ("Size", "L")]
		out = resolve_attribute_values(entries, attr_map)
		self.assertEqual(out, [
			{"attribute_id": "attr-color", "value": "Red"},
			{"attribute_id": "attr-size",  "value": "L"},
		])

	def test_skips_unknown_attribute(self):
		# An attribute that wasn't in attr_map is silently dropped (the caller
		# is expected to have called ensure_attributes_exist already).
		out = resolve_attribute_values([("Unknown", "X")], {})
		self.assertEqual(out, [])

	def test_skips_attribute_with_no_id(self):
		out = resolve_attribute_values([("Color", "Red")], {"Color": {}})
		self.assertEqual(out, [])

	def test_passes_through_value_even_if_not_in_values_map(self):
		# ``resolve_attribute_values`` doesn't validate that the value is in
		# possible_values — that's ``ensure_attributes_exist``'s job. Resolve
		# accepts any value for a known attribute name.
		attr_map = {"Color": {"id": "attr-color", "values": {"Red": "pv-red"}}}
		out = resolve_attribute_values([("Color", "Blau")], attr_map)
		self.assertEqual(out, [{"attribute_id": "attr-color", "value": "Blau"}])


if __name__ == "__main__":
	unittest.main()
