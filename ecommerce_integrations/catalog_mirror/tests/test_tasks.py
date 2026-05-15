"""Scheduler-entry smoke tests for catalog_mirror/tasks.py.

``sync_due_mirrors`` and ``recover_stale_mirrors`` are documented as
the hourly_long / hourly scheduler entries. They are *not* yet wired
into ``hooks.py`` (Phase 6 wiring is pending) but tests still need to
cover the contract:

- both are callable with no arguments,
- both return the documented summary dict shape,
- both no-op cleanly during install/migrate,
- both no-op when no mirrors exist on the site.

Live-apply behaviour is covered by the adapter tests and the resolver
suite; here we keep the scheduler-entry guard so renaming or breaking
the signature trips at test time.
"""

from __future__ import annotations

import unittest

import frappe
from frappe.tests import IntegrationTestCase

from ecommerce_integrations.catalog_mirror import tasks as catalog_mirror_tasks


class TestSchedulerEntryPointsExist(unittest.TestCase):
    """The names below are the scheduler hooks Phase 6 will wire up.
    Renaming or removing them silently is a regression."""

    REQUIRED_CALLABLES = (
        "sync_due_mirrors",
        "recover_stale_mirrors",
        "apply_mirror",
    )

    def test_all_required_names_exist(self):
        for name in self.REQUIRED_CALLABLES:
            self.assertTrue(
                callable(getattr(catalog_mirror_tasks, name, None)),
                f"catalog_mirror.tasks.{name} must remain callable",
            )


class TestSyncDueMirrorsContract(IntegrationTestCase):
    """``sync_due_mirrors`` walks every active mirror. The contract:

    - returns a dict with documented keys (total, ok, error, skipped),
    - no-ops cleanly when no mirrors exist,
    - no-ops during in_install / in_migrate.
    """

    def test_returns_summary_dict_with_documented_keys(self):
        out = catalog_mirror_tasks.sync_due_mirrors()
        self.assertIsInstance(out, dict)
        for key in ("total", "ok", "error", "skipped"):
            self.assertIn(key, out, f"missing key {key!r} in summary")

    def test_no_op_during_install(self):
        frappe.flags.in_install = True
        try:
            out = catalog_mirror_tasks.sync_due_mirrors()
            self.assertEqual(out, {"total": 0, "ok": 0, "error": 0, "skipped": 0})
        finally:
            frappe.flags.in_install = False

    def test_no_op_during_migrate(self):
        frappe.flags.in_migrate = True
        try:
            out = catalog_mirror_tasks.sync_due_mirrors()
            self.assertEqual(out, {"total": 0, "ok": 0, "error": 0, "skipped": 0})
        finally:
            frappe.flags.in_migrate = False


class TestRecoverStaleMirrors(IntegrationTestCase):
    """``recover_stale_mirrors`` sweeps mirrors stuck in ``running``
    past the heartbeat timeout. Contract: returns a dict with a
    ``recovered`` count, no-ops during install/migrate."""

    def test_returns_recovered_count(self):
        out = catalog_mirror_tasks.recover_stale_mirrors()
        self.assertIsInstance(out, dict)
        self.assertIn("recovered", out)
        self.assertIsInstance(out["recovered"], int)

    def test_no_op_during_install(self):
        frappe.flags.in_install = True
        try:
            self.assertEqual(
                catalog_mirror_tasks.recover_stale_mirrors(),
                {"recovered": 0},
            )
        finally:
            frappe.flags.in_install = False

    def test_no_op_during_migrate(self):
        frappe.flags.in_migrate = True
        try:
            self.assertEqual(
                catalog_mirror_tasks.recover_stale_mirrors(),
                {"recovered": 0},
            )
        finally:
            frappe.flags.in_migrate = False


class TestMappingFieldHelper(unittest.TestCase):
    """``_mapping_field`` is the single source of truth for which IG
    custom field a backend persists its category id into. Pure helper —
    safe to unit-test."""

    def test_known_backends_map(self):
        self.assertEqual(
            catalog_mirror_tasks._mapping_field("Shopware"),
            "shopware_category_id",
        )
        self.assertEqual(
            catalog_mirror_tasks._mapping_field("Medusa"),
            "medusa_category_id",
        )

    def test_unknown_backend_returns_none(self):
        self.assertIsNone(catalog_mirror_tasks._mapping_field("NotABackend"))


if __name__ == "__main__":
    unittest.main()
