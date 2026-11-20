"""Delivery write-back: partial shipments and merged tracking codes."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


class TestDeliveryStatus(unittest.TestCase):
    def test_split_tracking_codes(self):
        from ecommerce_integrations.shopware6.status_sync import split_tracking_codes
        self.assertEqual(split_tracking_codes("A1, B2;A1\nC3"), ["A1", "B2", "C3"])
        self.assertEqual(split_tracking_codes(None), [])
        self.assertEqual(split_tracking_codes(["X", "X", " "]), ["X"])

    def _submit(self, per_delivered):
        from ecommerce_integrations.shopware6 import status_sync as ss
        doc = SimpleNamespace(name="DN-1", lr_no="A1", items=[SimpleNamespace(against_sales_order="SO-1")])
        values = {"shopware_order_id": "ord-1", "per_delivered": per_delivered}
        with patch.object(ss, "is_shopware_enabled", return_value=True), \
             patch.object(ss.frappe.db, "get_value", side_effect=lambda dt, n, f: values.get(f)), \
             patch.object(ss.frappe, "enqueue") as enq, \
             patch.object(ss, "create_shopware_log"), \
             patch.object(ss, "ORDER_ID_FIELD", "shopware_order_id"), \
             patch.object(ss.frappe, "get_cached_doc", return_value=SimpleNamespace(auto_create_invoice_on_shipment=0)):
            ss.on_delivery_note_submit(doc)
        return enq.call_args_list[0].kwargs["action"]

    def test_full_and_partial(self):
        self.assertEqual(self._submit(100), "ship")
        self.assertEqual(self._submit(40), "ship_partially")

    def test_tracking_codes_are_merged(self):
        from ecommerce_integrations.shopware6 import status_sync as ss
        client = MagicMock()
        client.request_post.return_value = SimpleNamespace(data=[{"id": "d1", "trackingCodes": ["OLD"]}])
        fn = getattr(ss.update_shopware_delivery_status, "__wrapped__", None)
        if fn is None:
            self.skipTest("decorator does not expose __wrapped__")
        self.assertTrue(fn(client, "ord-1", None, tracking_number="NEW,OLD"))
        client.request_patch.assert_called_once_with("order-delivery/d1", {"trackingCodes": ["OLD", "NEW"]})
        client.request_post.assert_called_once()  # search only, no state change
