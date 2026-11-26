"""The payment webhook acts on the state Shopware reports, not on the payload."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


class TestPaymentWebhookVerify(unittest.TestCase):
    def _run(self, verified):
        from ecommerce_integrations.shopware6 import payment as p
        payload = {"data": {"orderId": "o1", "transactionId": "t1", "state": "paid"}}
        with patch.object(p, "fetch_transaction_state", return_value=verified), \
             patch.object(p, "update_shopware_log") as log, \
             patch.object(p.frappe, "set_user"), \
             patch.object(p.frappe.db, "get_value", return_value="SO-1"), \
             patch.object(p.frappe.db, "set_value") as setv, \
             patch.object(p.frappe, "get_doc", return_value=SimpleNamespace(sync_sales_invoice=0)), \
             patch.object(p, "create_payment_entry_for_order", return_value=None) as pe:
            p.handle_transaction_state_change(payload, request_id="r1")
        return log, setv, pe

    def test_not_found_is_skipped(self):
        log, setv, pe = self._run({"missing": True})
        setv.assert_not_called()
        pe.assert_not_called()
        self.assertEqual(log.call_args.kwargs["status"], "Skipped")

    def test_disabled_integration_is_retryable_error(self):
        log, setv, pe = self._run(None)
        pe.assert_not_called()
        self.assertEqual(log.call_args.kwargs["status"], "Error")

    def test_unreachable_is_retryable_error(self):
        from ecommerce_integrations.shopware6 import payment as p
        log, setv, pe = self._run({"error": "timeout"})
        setv.assert_not_called()
        pe.assert_not_called()
        self.assertEqual(log.call_args.kwargs["status"], "Error")
        self.assertIn(p._UNVERIFIED, log.call_args.kwargs["message"])

    def test_shopware_state_wins(self):
        _log, _setv, pe = self._run({"state": "open", "transaction_id": "t1"})
        pe.assert_not_called()

    def test_verified_paid_books(self):
        _log, _setv, pe = self._run({"state": "paid", "transaction_id": "t1"})
        pe.assert_called_once_with("SO-1", "t1")

    def test_fetch_rejects_foreign_transaction(self):
        from ecommerce_integrations.shopware6 import payment as p
        client = MagicMock()
        client.request_post.return_value = SimpleNamespace(data=[{"id": "t1", "orderId": "other", "stateMachineState": {"technicalName": "paid"}}])
        self.assertEqual(p.fetch_transaction_state.__wrapped__(client, order_id="o1", transaction_id="t1"),
                         {"missing": True})
        client.request_post.return_value = SimpleNamespace(data=[{"id": "t1", "orderId": "o1", "stateMachineState": {"technicalName": "paid"}}])
        self.assertEqual(p.fetch_transaction_state.__wrapped__(client, order_id="o1", transaction_id="t1"),
                         {"state": "paid", "transaction_id": "t1"})
