"""Smoke tests for ai_description/services/*.

``access.py`` and ``logging.py`` are the shared service helpers every
AI-description API entry point routes through:

- ``require_ai_admin`` enforces the "only System Manager" rule on
  administrative endpoints (batch generate, settings summary).
- ``get_item_with_permission`` is the read/write-permission gate for
  per-item endpoints.
- ``clamp_limit`` clamps user-provided page sizes.
- ``truncate_for_log`` keeps prompt/response storage bounded.

These are pure helpers; we test them at unit level. Live calls into
Frappe permission machinery are covered by the IntegrationTestCase
fixtures.
"""

from __future__ import annotations

import unittest

from ecommerce_integrations.ai_description.services import access, logging


class TestClampLimit(unittest.TestCase):
    """``clamp_limit`` keeps user-provided page sizes inside a safe
    range. Pure helper; values outside [1, 500] should be clamped, not
    raised on."""

    def test_within_range_returns_as_is(self):
        self.assertEqual(access.clamp_limit(50), 50)

    def test_below_minimum_clamps_up(self):
        self.assertEqual(access.clamp_limit(0), 1)
        self.assertEqual(access.clamp_limit(-100), 1)

    def test_above_maximum_clamps_down(self):
        self.assertEqual(access.clamp_limit(10000), 500)

    def test_custom_bounds(self):
        self.assertEqual(access.clamp_limit(50, minimum=10, maximum=20), 20)

    def test_accepts_string_int(self):
        # The caller often passes user-provided strings — int() the value.
        self.assertEqual(access.clamp_limit("25"), 25)


class TestTruncateForLog(unittest.TestCase):
    """Log payloads that exceed the configured limit must be truncated
    with a marker, not stored raw — the doctype's text column has a
    bound and an unbounded prompt blob would silently drop other fields."""

    def test_short_input_unchanged(self):
        self.assertEqual(logging.truncate_for_log("hello", limit=100), "hello")

    def test_long_input_truncated_with_marker(self):
        long_blob = "x" * 500
        out = logging.truncate_for_log(long_blob, limit=100)
        self.assertTrue(out.endswith("... [truncated]"))
        # The marker adds 15 chars; the prefix should be exactly the limit.
        self.assertEqual(len(out), 100 + len("... [truncated]"))

    def test_at_limit_unchanged(self):
        s = "x" * 100
        self.assertEqual(logging.truncate_for_log(s, limit=100), s)


class TestAccessHelpersPublicSurface(unittest.TestCase):
    """The service helpers are imported by the api module and the
    scheduler. Renaming or dropping them is a breaking change."""

    REQUIRED_CALLABLES = (
        (access, "require_ai_admin"),
        (access, "get_item_with_permission"),
        (access, "clamp_limit"),
        (logging, "truncate_for_log"),
        (logging, "create_ai_description_log"),
        (logging, "mark_ai_description_log_success"),
        (logging, "mark_ai_description_log_failed"),
    )

    def test_all_required_names_exist(self):
        for module, name in self.REQUIRED_CALLABLES:
            self.assertTrue(
                callable(getattr(module, name, None)),
                f"{module.__name__}.{name} must remain callable",
            )


if __name__ == "__main__":
    unittest.main()
