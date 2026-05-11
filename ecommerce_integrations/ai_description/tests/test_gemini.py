"""Tests for ai_description/gemini.py pure helpers.

``parse_ai_response`` is the one piece of gemini.py that's safe to test
without an API key — it transforms a string into a dict and has no side
effects. The rest (``generate_description`` etc.) need a real Gemini
client; those are exercised by the integration suite once
GOOGLE_API_KEY is configured on the site.
"""

import unittest

from ecommerce_integrations.ai_description.gemini import parse_ai_response


class TestParseAiResponse(unittest.TestCase):
	def test_plain_json(self):
		out = parse_ai_response('{"description": "hi"}')
		self.assertEqual(out, {"description": "hi"})

	def test_json_in_markdown_code_block(self):
		out = parse_ai_response('```json\n{"description": "hi"}\n```')
		self.assertEqual(out, {"description": "hi"})

	def test_json_in_plain_code_block(self):
		out = parse_ai_response('```\n{"description": "hi"}\n```')
		self.assertEqual(out, {"description": "hi"})

	def test_json_embedded_in_prose(self):
		# Falls back to greedy "find the first {...}" — useful for chatty
		# models that decline to stop after the JSON.
		out = parse_ai_response('Sure! Here is the JSON: {"description": "hi"}\n')
		self.assertEqual(out, {"description": "hi"})

	def test_unparseable_returns_error_dict(self):
		out = parse_ai_response("not json at all")
		self.assertIn("error", out)
		self.assertIn("raw_response", out)

	def test_unparseable_truncates_raw_response(self):
		# Truncated to 1000 chars to keep the error log reasonable when the
		# model goes off the rails and returns a huge non-JSON blob.
		long_blob = "x" * 5000
		out = parse_ai_response(long_blob)
		self.assertLessEqual(len(out["raw_response"]), 1000)

	def test_nested_json_structure(self):
		raw = '```json\n{"sections": [{"title": "A", "body": "B"}]}\n```'
		out = parse_ai_response(raw)
		self.assertEqual(out["sections"][0]["title"], "A")


class TestAiDescriptionPublicSurface(unittest.TestCase):
	"""Whitelisted endpoints and the scheduler entrypoint must remain
	importable; UI code and hooks.py reference them by dotted path."""

	def test_api_endpoints_importable(self):
		from ecommerce_integrations.ai_description import api

		for name in (
			"generate_description_for_item",
			"generate_descriptions_batch",
			"get_pending_items",
			"get_generation_status",
			"regenerate_description",
			"mark_as_reviewed",
			"get_ai_settings_summary",
			"copy_ai_to_main_description",
			"sync_ai_description_to_shopware",
		):
			self.assertTrue(
				callable(getattr(api, name, None)),
				f"ai_description.api.{name} must be callable",
			)

	def test_scheduler_entrypoint_importable(self):
		from ecommerce_integrations.ai_description.scheduler import (
			process_batch_descriptions,
		)
		self.assertTrue(callable(process_batch_descriptions))


if __name__ == "__main__":
	unittest.main()
