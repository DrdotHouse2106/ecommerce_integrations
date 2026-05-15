"""Smoke tests for controllers/customer.py.

``EcommerceCustomer`` is the shared customer-create / merge class used
by both Shopify (upstream) and the fork channels (shopware6, medusa).
The mapping field (``shopify_customer_id``, ``shopware6_customer_id``,
``medusa_customer_id``) is per-integration; the class itself is generic.

Tests cover the lossless round-trip:

- ``is_synced`` returns False for an unknown external id,
- ``sync_customer`` creates an ERPNext Customer with the supplied
  customer_id as the document name (the autoname override is
  load-bearing — without it concurrent webhooks duplicate),
- a second ``sync_customer`` call with the same id is idempotent
  (no DuplicateEntryError leaks out).

Generic placeholders only — ``CUST-001`` etc.
"""

from __future__ import annotations

import unittest

import frappe
from frappe.tests import IntegrationTestCase

from ecommerce_integrations.controllers.customer import EcommerceCustomer

# We need a custom Link field on Customer for the test. ``shopware6_customer_id``
# exists on every install with the fork (added by patches). Use that.
_TEST_ID_FIELD = "shopware6_customer_id"
_TEST_INTEGRATION = "shopware6"


class TestEcommerceCustomer(IntegrationTestCase):
    """Real DB tests for the customer round-trip. We never reach the
    network here — ``EcommerceCustomer`` is local-only and only writes
    to ERPNext."""

    TEST_CUSTOMER_ID = "CUST-001-TEST-EC"
    TEST_CUSTOMER_NAME = "Demo Kunde GmbH"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if not frappe.db.has_column("Customer", _TEST_ID_FIELD):
            # The shopware6 custom field hasn't been installed on this
            # site — skip the entire class rather than per-test.
            raise unittest.SkipTest(
                f"Customer.{_TEST_ID_FIELD} not present on this site"
            )

    def setUp(self):
        self._wipe()

    def tearDown(self):
        self._wipe()

    def _wipe(self):
        for name in frappe.get_all(
            "Customer",
            filters={_TEST_ID_FIELD: self.TEST_CUSTOMER_ID},
            pluck="name",
        ):
            try:
                frappe.delete_doc(
                    "Customer", name, force=True, ignore_permissions=True,
                )
            except Exception:
                pass
        # Also clean up by name match (sync_customer sets name == id).
        if frappe.db.exists("Customer", self.TEST_CUSTOMER_ID):
            try:
                frappe.delete_doc(
                    "Customer", self.TEST_CUSTOMER_ID,
                    force=True, ignore_permissions=True,
                )
            except Exception:
                pass

    def _make_subject(self):
        return EcommerceCustomer(
            customer_id=self.TEST_CUSTOMER_ID,
            customer_id_field=_TEST_ID_FIELD,
            integration=_TEST_INTEGRATION,
        )

    def test_is_synced_false_for_unknown(self):
        subject = self._make_subject()
        self.assertFalse(subject.is_synced())

    def test_sync_creates_customer_with_id_as_name(self):
        """The id is used as the document name (bypasses autoname) so
        concurrent webhooks don't race onto the autoname series."""
        # Pick a customer group that exists — most installs ship "All
        # Customer Groups" as the root.
        group = frappe.db.get_value(
            "Customer Group", {"is_group": 0}, "name"
        ) or "Individual"

        subject = self._make_subject()
        subject.sync_customer(
            customer_name=self.TEST_CUSTOMER_NAME,
            customer_group=group,
        )
        # Must exist with name == TEST_CUSTOMER_ID.
        self.assertTrue(frappe.db.exists("Customer", self.TEST_CUSTOMER_ID))
        self.assertEqual(
            frappe.db.get_value("Customer", self.TEST_CUSTOMER_ID, "customer_name"),
            self.TEST_CUSTOMER_NAME,
        )
        # And is_synced now reports True.
        self.assertTrue(subject.is_synced())

    def test_sync_is_idempotent(self):
        """A second sync_customer call with the same id must not raise
        DuplicateEntryError — it should silently re-stamp the id field."""
        group = frappe.db.get_value(
            "Customer Group", {"is_group": 0}, "name"
        ) or "Individual"

        subject = self._make_subject()
        subject.sync_customer(
            customer_name=self.TEST_CUSTOMER_NAME,
            customer_group=group,
        )
        # Second call must not raise.
        subject.sync_customer(
            customer_name=self.TEST_CUSTOMER_NAME,
            customer_group=group,
        )
        self.assertTrue(frappe.db.exists("Customer", self.TEST_CUSTOMER_ID))
