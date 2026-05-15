"""Scheduler-entry smoke tests for smart_collections/tasks.py.

``sync_due_collections`` and ``recover_stale_targets`` are wired onto
the periodic scheduler. The contract:

- both are callable with no arguments,
- ``recover_stale_targets`` returns ``{"recovered": int}`` and no-ops
  during install / migrate,
- ``sync_due_collections`` returns None and no-ops during install /
  migrate,
- ``sync_all_collections`` returns a summary dict with the documented
  keys.

The live sync path is exercised end-to-end in ``test_preview.py``; here
we keep the scheduler-entry guard so renaming or breaking the signature
trips at test time.
"""

from __future__ import annotations

import unittest

import frappe
from frappe.tests import IntegrationTestCase

from ecommerce_integrations.smart_collections import tasks as smart_collections_tasks


class TestSchedulerEntryPointsExist(unittest.TestCase):
    """The names below are the scheduler hooks declared on hooks.py.
    Renaming or removing them silently is a regression for installed sites."""

    REQUIRED_CALLABLES = (
        "sync_due_collections",
        "recover_stale_targets",
        "sync_all_collections",
        "sync_collection",
        "sync_collection_now",
    )

    def test_all_required_names_exist(self):
        for name in self.REQUIRED_CALLABLES:
            self.assertTrue(
                callable(getattr(smart_collections_tasks, name, None)),
                f"smart_collections.tasks.{name} must remain callable",
            )


class TestSyncDueCollections(IntegrationTestCase):
    """``sync_due_collections`` runs every active collection on every
    backend. Contract: no return value, no-ops during install/migrate."""

    def test_no_op_during_install(self):
        frappe.flags.in_install = True
        try:
            # Must not raise and must not block — the scheduler tolerates
            # missing tables on a fresh install.
            self.assertIsNone(smart_collections_tasks.sync_due_collections())
        finally:
            frappe.flags.in_install = False

    def test_no_op_during_migrate(self):
        frappe.flags.in_migrate = True
        try:
            self.assertIsNone(smart_collections_tasks.sync_due_collections())
        finally:
            frappe.flags.in_migrate = False


class TestRecoverStaleTargets(IntegrationTestCase):
    """``recover_stale_targets`` sweeps targets stuck in ``running``
    past the heartbeat timeout. Contract: dict with a ``recovered``
    count, no-ops during install/migrate."""

    def test_returns_recovered_count(self):
        out = smart_collections_tasks.recover_stale_targets()
        self.assertIsInstance(out, dict)
        self.assertIn("recovered", out)
        self.assertIsInstance(out["recovered"], int)

    def test_no_op_during_install(self):
        frappe.flags.in_install = True
        try:
            self.assertEqual(
                smart_collections_tasks.recover_stale_targets(),
                {"recovered": 0},
            )
        finally:
            frappe.flags.in_install = False


class TestSyncAllCollectionsContract(IntegrationTestCase):
    """``sync_all_collections`` returns a documented summary dict. The
    backend filter is optional; the function must accept both forms."""

    def test_returns_summary_dict(self):
        out = smart_collections_tasks.sync_all_collections()
        self.assertIsInstance(out, dict)
        for key in ("total", "ok", "skipped", "error"):
            self.assertIn(key, out)

    def test_accepts_backend_filter(self):
        # Should not raise — filter just restricts to one backend's targets.
        out = smart_collections_tasks.sync_all_collections(backend="Shopware")
        self.assertIsInstance(out, dict)


if __name__ == "__main__":
    unittest.main()
