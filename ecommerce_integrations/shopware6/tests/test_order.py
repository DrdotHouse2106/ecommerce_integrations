"""Tests for ShopwareOrder + pure helpers in order/tax_handler,
order/delivery_handler and order/scheduled_sync."""

import unittest
from unittest.mock import patch, MagicMock

from ecommerce_integrations.shopware6.tests.utils import ShopwareTestCase


class TestShopwareOrderInit(ShopwareTestCase):
	"""Construction-time behaviour. Full sync is integration-only."""

	@patch("ecommerce_integrations.shopware6.order.order_sync.frappe")
	def test_initialization_stores_order_id(self, mock_frappe):
		from ecommerce_integrations.shopware6.order.order_sync import ShopwareOrder

		mock_frappe.get_cached_doc.return_value = self.make_enabled_setting_mock()
		mock_frappe.db.get_value.return_value = None  # no prior SO

		order = ShopwareOrder(order_id="test-order-123")
		self.assertEqual(order.order_id, "test-order-123")
		self.assertIsNone(order.sales_order_name)

	@patch("ecommerce_integrations.shopware6.order.order_sync.frappe")
	def test_is_synced_false_when_no_prior_so(self, mock_frappe):
		from ecommerce_integrations.shopware6.order.order_sync import ShopwareOrder

		mock_frappe.get_cached_doc.return_value = self.make_enabled_setting_mock()
		mock_frappe.db.get_value.return_value = None

		order = ShopwareOrder(order_id="new-order-123")
		self.assertFalse(order.is_synced())

	@patch("ecommerce_integrations.shopware6.order.order_sync.frappe")
	def test_is_synced_true_when_prior_so_exists(self, mock_frappe):
		from ecommerce_integrations.shopware6.order.order_sync import ShopwareOrder

		mock_frappe.get_cached_doc.return_value = self.make_enabled_setting_mock()
		mock_frappe.db.get_value.return_value = "SO-0001"

		order = ShopwareOrder(order_id="synced-order-123")
		self.assertTrue(order.is_synced())
		self.assertEqual(order.sales_order_name, "SO-0001")


class TestTaxHandler(unittest.TestCase):
	"""Pure helpers from ``shopware6.order.tax_handler``."""

	def test_collect_tax_data_groups_by_rate(self):
		from ecommerce_integrations.shopware6.order.tax_handler import collect_tax_data

		# Two line items with the same 19% rate plus one with 7% — collect
		# should bucket them by rate.
		order_data = {
			"lineItems": [
				{"taxRules": [{"taxRate": 19.0}], "price": {"calculatedTaxes": [{"taxRate": 19.0, "tax": 1.90, "price": 10.0}]}},
				{"taxRules": [{"taxRate": 19.0}], "price": {"calculatedTaxes": [{"taxRate": 19.0, "tax": 3.80, "price": 20.0}]}},
				{"taxRules": [{"taxRate": 7.0}], "price": {"calculatedTaxes": [{"taxRate": 7.0, "tax": 0.35, "price": 5.0}]}},
			],
		}
		result = collect_tax_data(order_data)
		self.assertIn(19.0, result)
		self.assertIn(7.0, result)

	def test_collect_tax_data_empty_order(self):
		from ecommerce_integrations.shopware6.order.tax_handler import collect_tax_data

		# No line items → empty dict, not a crash.
		self.assertEqual(collect_tax_data({"lineItems": []}), {})
		self.assertEqual(collect_tax_data({}), {})


class TestDeliveryHandler(unittest.TestCase):
	"""``get_shipping_address_from_delivery`` maps Shopware fields onto
	ERPNext Address fields (address_line1, city, pincode, …)."""

	def test_get_shipping_address_maps_fields(self):
		from ecommerce_integrations.shopware6.order.delivery_handler import (
			get_shipping_address_from_delivery,
		)
		delivery = {
			"shippingOrderAddress": {
				"street": "Musterstraße 1",
				"additionalAddressLine1": "Hinterhaus",
				"city": "Berlin",
				"zipcode": "10115",
				"country": {"name": "Germany"},
				"countryState": {"name": "Berlin"},
			},
		}
		result = get_shipping_address_from_delivery(delivery)
		self.assertEqual(result["address_line1"], "Musterstraße 1")
		self.assertEqual(result["address_line2"], "Hinterhaus")
		self.assertEqual(result["city"], "Berlin")
		self.assertEqual(result["pincode"], "10115")
		self.assertEqual(result["country"], "Germany")
		self.assertEqual(result["state"], "Berlin")

	def test_get_shipping_address_returns_blank_when_missing(self):
		from ecommerce_integrations.shopware6.order.delivery_handler import (
			get_shipping_address_from_delivery,
		)
		# Empty delivery yields a fully-blank Address dict — keeps downstream
		# code from having to handle KeyError on every field access.
		result = get_shipping_address_from_delivery({})
		self.assertEqual(result["address_line1"], "")
		self.assertEqual(result["country"], "")

	def test_get_shipping_method_name(self):
		from ecommerce_integrations.shopware6.order.delivery_handler import (
			get_shipping_method_name,
		)
		delivery = {"shippingMethod": {"name": "DHL Express"}}
		self.assertEqual(get_shipping_method_name(delivery), "DHL Express")

	def test_get_shipping_method_name_none_when_missing(self):
		from ecommerce_integrations.shopware6.order.delivery_handler import (
			get_shipping_method_name,
		)
		self.assertIsNone(get_shipping_method_name({}))


class TestScheduledSync(ShopwareTestCase):
	"""Scheduler entry-point: gating on enabled-state and lock semantics."""

	@staticmethod
	def _setup_running_scheduler(mock_frappe, *, lock_acquired: bool):
		"""Common scaffolding: enabled setting + frequency + lock mock."""
		setting = ShopwareTestCase.make_enabled_setting_mock(
			order_sync_frequency=60, last_order_sync=None,
		)
		mock_frappe.get_doc.return_value = setting
		lock = MagicMock()
		lock.acquire.return_value = lock_acquired
		mock_frappe.cache.return_value.lock.return_value = lock
		return lock

	@patch("ecommerce_integrations.shopware6.order.scheduled_sync.frappe")
	def test_skipped_when_integration_disabled(self, mock_frappe):
		from ecommerce_integrations.shopware6.order.scheduled_sync import (
			scheduled_order_sync,
		)
		setting = MagicMock()
		setting.is_enabled.return_value = False
		mock_frappe.get_doc.return_value = setting

		self.assertIsNone(scheduled_order_sync())
		mock_frappe.cache.assert_not_called()

	@patch("ecommerce_integrations.shopware6.order.scheduled_sync.sync_orders_from_shopware")
	@patch("ecommerce_integrations.shopware6.order.scheduled_sync.frappe")
	def test_skipped_when_lock_already_held(self, mock_frappe, mock_sync):
		from ecommerce_integrations.shopware6.order.scheduled_sync import (
			scheduled_order_sync,
		)
		lock = self._setup_running_scheduler(mock_frappe, lock_acquired=False)

		scheduled_order_sync()
		mock_sync.assert_not_called()
		lock.release.assert_not_called()

	@patch("ecommerce_integrations.shopware6.order.scheduled_sync.sync_orders_from_shopware")
	@patch("ecommerce_integrations.shopware6.order.scheduled_sync.frappe")
	def test_lock_released_on_success(self, mock_frappe, mock_sync):
		from ecommerce_integrations.shopware6.order.scheduled_sync import (
			scheduled_order_sync,
		)
		lock = self._setup_running_scheduler(mock_frappe, lock_acquired=True)
		mock_sync.return_value = {"synced": 1, "errors": 0}

		scheduled_order_sync()
		mock_sync.assert_called_once()
		mock_frappe.db.commit.assert_called_once()
		lock.release.assert_called_once()

	@patch("ecommerce_integrations.shopware6.order.scheduled_sync.sync_orders_from_shopware")
	@patch("ecommerce_integrations.shopware6.order.scheduled_sync.frappe")
	def test_lock_released_on_error(self, mock_frappe, mock_sync):
		from ecommerce_integrations.shopware6.order.scheduled_sync import (
			scheduled_order_sync,
		)
		lock = self._setup_running_scheduler(mock_frappe, lock_acquired=True)
		mock_sync.side_effect = RuntimeError("boom")

		scheduled_order_sync()
		mock_frappe.db.rollback.assert_called_once()
		lock.release.assert_called_once()


if __name__ == "__main__":
	unittest.main()
