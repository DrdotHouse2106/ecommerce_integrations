"""Smoke tests for controllers/scheduling.py.

``need_to_run`` is the shared "configurable scheduled event" gate used
by Shopify and the fork channels. It writes the current timestamp back
onto the Setting doctype each time it returns True, so it's mildly
mutating and needs a real DB — IntegrationTestCase.

Tests cover the documented behaviour:

- first call returns True when no last_run is set,
- second call within the interval returns False,
- a stale last_run lets the next call return True again.

We use ``Shopware Setting`` as the test fixture because it exists on
the fork. Reads/writes use generic field names that exist on the
doctype (the ``order_sync_frequency`` / ``last_order_sync`` pair).
"""

from __future__ import annotations

import unittest

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_to_date, now_datetime

from ecommerce_integrations.controllers.scheduling import need_to_run

_SETTING = "Shopware Setting"
_INTERVAL_FIELD = "order_sync_frequency"
_TIMESTAMP_FIELD = "last_order_sync"


class TestNeedToRun(IntegrationTestCase):
    """Sequence: clear → first call returns True; second call (within
    the interval) returns False; setting last_run into the past lets it
    return True again."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if not frappe.db.exists("DocType", _SETTING):
            raise unittest.SkipTest(f"{_SETTING} not present on this site")

    def setUp(self):
        # Reset the timestamp so each test starts from a known state.
        try:
            frappe.db.set_value(
                _SETTING, None, _TIMESTAMP_FIELD, None,
                update_modified=False,
            )
            # Ensure an interval is configured so the gate has something
            # to compare against.
            current_interval = frappe.db.get_single_value(_SETTING, _INTERVAL_FIELD)
            if not current_interval:
                frappe.db.set_value(
                    _SETTING, None, _INTERVAL_FIELD, 30,
                    update_modified=False,
                )
        except Exception as e:
            self.skipTest(f"Could not configure {_SETTING}: {e}")

    def test_returns_true_on_first_call(self):
        self.assertTrue(need_to_run(_SETTING, _INTERVAL_FIELD, _TIMESTAMP_FIELD))

    def test_returns_false_within_interval(self):
        # First call sets the timestamp; second call within the interval
        # must return False so the scheduler doesn't double-fire.
        self.assertTrue(need_to_run(_SETTING, _INTERVAL_FIELD, _TIMESTAMP_FIELD))
        self.assertFalse(need_to_run(_SETTING, _INTERVAL_FIELD, _TIMESTAMP_FIELD))

    def test_returns_true_after_interval_lapses(self):
        # First call → True, sets timestamp. Roll the timestamp back past
        # the interval window. Next call → True again.
        self.assertTrue(need_to_run(_SETTING, _INTERVAL_FIELD, _TIMESTAMP_FIELD))
        # Set timestamp 1 hour in the past — well past any sensible
        # default interval.
        frappe.db.set_value(
            _SETTING, None, _TIMESTAMP_FIELD,
            add_to_date(now_datetime(), hours=-1),
            update_modified=False,
        )
        self.assertTrue(need_to_run(_SETTING, _INTERVAL_FIELD, _TIMESTAMP_FIELD))
