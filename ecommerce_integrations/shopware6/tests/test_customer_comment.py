"""Tests for the customer order comment -> fulfillment hint mapping.

Covers the shared normaliser / highlight logic in
``ecommerce_integrations/customer_comment.py``, the Shopware extraction in
``order_mapper.extract_customer_comment`` and the update-path helpers in
``order_sync``.
"""

from unittest.mock import MagicMock, patch

import frappe

from ecommerce_integrations.ecommerce_integrations import customer_comment as cc
from ecommerce_integrations.shopware6.order.order_mapper import extract_customer_comment
from ecommerce_integrations.shopware6.tests.utils import ShopwareTestCase, create_sample_order_data

PICKUP_COMMENT = "ABHOLUNG: Oldtimertreffen Musterstadt, Musterstadt, 17.07.2027"


class TestNormalizeCustomerComment(ShopwareTestCase):
	def test_trims_and_keeps_line_breaks(self):
		self.assertEqual(
			cc.normalize_customer_comment("  Bitte klingeln \r\n\r\n\r\n\r\nDanke  "),
			"Bitte klingeln\n\nDanke",
		)

	def test_none_and_non_string(self):
		self.assertEqual(cc.normalize_customer_comment(None), "")
		self.assertEqual(cc.normalize_customer_comment(""), "")
		self.assertEqual(cc.normalize_customer_comment(12345), "12345")

	def test_strips_markup_to_plain_text(self):
		raw = "<p>Hallo <b>Lager</b></p><script>alert(1)</script><br/>Tor 2"
		self.assertEqual(cc.normalize_customer_comment(raw), "Hallo Lager\nalert(1)\nTor 2")

	def test_decodes_entities_and_drops_double_encoded_tags(self):
		raw = "Gr&uuml;&szlig;e &amp; Dank &lt;img src=x onerror=alert(1)&gt;"
		self.assertEqual(cc.normalize_customer_comment(raw), "Grüße & Dank")

	def test_keeps_stray_angle_bracket_text(self):
		self.assertEqual(cc.normalize_customer_comment("Menge < 5 Stück"), "Menge < 5 Stück")

	def test_removes_control_characters(self):
		self.assertEqual(cc.normalize_customer_comment("A\x00B\x07C\tD"), "ABC D")

	def test_length_cap(self):
		result = cc.normalize_customer_comment("x" * 5000, max_length=100)
		self.assertEqual(len(result), 100)
		self.assertTrue(result.endswith("…"))


class TestHighlight(ShopwareTestCase):
	def test_default_prefix(self):
		self.assertTrue(cc.is_highlighted(PICKUP_COMMENT, cc.DEFAULT_HIGHLIGHT_PREFIXES))

	def test_case_insensitive_and_leading_whitespace(self):
		self.assertTrue(cc.is_highlighted("  abholung: Messe", ["ABHOLUNG:"]))

	def test_prefix_must_be_at_start(self):
		self.assertFalse(cc.is_highlighted("Bitte keine ABHOLUNG: senden", ["ABHOLUNG:"]))

	def test_empty(self):
		self.assertFalse(cc.is_highlighted("", ["ABHOLUNG:"]))
		self.assertFalse(cc.is_highlighted(PICKUP_COMMENT, []))

	def test_prefixes_from_site_config(self):
		with patch.object(
			cc.frappe, "conf", frappe._dict({cc.HIGHLIGHT_PREFIXES_CONFIG_KEY: "PICKUP:, EXPRESS:"})
		):
			self.assertEqual(cc.get_highlight_prefixes(), ("PICKUP:", "EXPRESS:"))
			self.assertTrue(cc.is_highlighted("express: heute raus"))
			self.assertFalse(cc.is_highlighted(PICKUP_COMMENT))

	def test_prefixes_default_when_not_configured(self):
		with patch.object(cc.frappe, "conf", frappe._dict()):
			self.assertEqual(cc.get_highlight_prefixes(), cc.DEFAULT_HIGHLIGHT_PREFIXES)

	def test_empty_site_config_disables(self):
		with patch.object(cc.frappe, "conf", frappe._dict({cc.HIGHLIGHT_PREFIXES_CONFIG_KEY: []})):
			self.assertEqual(cc.get_highlight_prefixes(), ())


class TestCombineComments(ShopwareTestCase):
	def test_single_order_plain(self):
		self.assertEqual(cc.combine_comments({"SO-00001": "Tor 2"}), "Tor 2")

	def test_multiple_orders_labelled(self):
		result = cc.combine_comments({"SO-00001": "Tor 2", "SO-00002": "", "SO-00003": PICKUP_COMMENT})
		self.assertEqual(result, f"[SO-00001] Tor 2\n\n[SO-00003] {PICKUP_COMMENT}")

	def test_no_comments(self):
		self.assertEqual(cc.combine_comments({"SO-00001": "", "SO-00002": ""}), "")


class TestShopwareExtraction(ShopwareTestCase):
	def test_from_order(self):
		order = create_sample_order_data()
		order["customerComment"] = f"  {PICKUP_COMMENT}\n"
		self.assertEqual(extract_customer_comment(order), PICKUP_COMMENT)

	def test_from_webhook_entity(self):
		payload = {"payload": {"entity": {"customerComment": "<b>Tor 2</b>"}}}
		self.assertEqual(extract_customer_comment(payload), "Tor 2")

	def test_missing(self):
		self.assertEqual(extract_customer_comment(create_sample_order_data()), "")
		self.assertEqual(extract_customer_comment({"customerComment": None}), "")
		self.assertEqual(extract_customer_comment({}), "")


class TestShopwareUpdatePath(ShopwareTestCase):
	def test_comment_from_payload_shapes(self):
		from ecommerce_integrations.shopware6.order.order_sync import _comment_from_payload

		self.assertEqual(_comment_from_payload({"data": {"customerComment": "A"}}), (True, "A"))
		self.assertEqual(_comment_from_payload({"payload": {"entity": {"customerComment": ""}}}), (True, ""))
		self.assertEqual(_comment_from_payload({"customerComment": "C"}), (True, "C"))
		self.assertEqual(_comment_from_payload({"data": {"isUpdate": True}}), (False, None))

	def test_update_may_touch_comment(self):
		from ecommerce_integrations.shopware6.order.order_sync import _update_may_touch_comment

		self.assertTrue(_update_may_touch_comment({"data": {"isUpdate": True}}))
		self.assertFalse(
			_update_may_touch_comment(
				{"data": {"payload": [{"entity": "order", "updatedFields": ["stateId"]}]}}
			)
		)
		self.assertTrue(
			_update_may_touch_comment(
				{"data": {"payload": [{"entity": "order", "updatedFields": ["customerComment"]}]}}
			)
		)

	# update_sales_order_comment is mocked to report "unchanged" so the
	# helper does not commit inside the test transaction.
	@patch("ecommerce_integrations.shopware6.order.order_sync.update_sales_order_comment", return_value=False)
	@patch("ecommerce_integrations.shopware6.order.order_sync._fetch_customer_comment")
	def test_refresh_uses_payload_without_api_call(self, mock_fetch, mock_update):
		from ecommerce_integrations.shopware6.order.order_sync import refresh_customer_comment

		refresh_customer_comment("SO-00001", "order-id", {"data": {"customerComment": PICKUP_COMMENT}})
		mock_fetch.assert_not_called()
		mock_update.assert_called_once_with("SO-00001", PICKUP_COMMENT)

	@patch("ecommerce_integrations.shopware6.order.order_sync.update_sales_order_comment", return_value=False)
	@patch("ecommerce_integrations.shopware6.order.order_sync._fetch_customer_comment", return_value="Tor 2")
	def test_refresh_fetches_when_payload_lacks_value(self, mock_fetch, mock_update):
		from ecommerce_integrations.shopware6.order.order_sync import refresh_customer_comment

		refresh_customer_comment("SO-00001", "order-id", {"data": {"isUpdate": True}})
		mock_fetch.assert_called_once_with("order-id")
		mock_update.assert_called_once_with("SO-00001", "Tor 2")

	@patch(
		"ecommerce_integrations.shopware6.order.order_sync._fetch_customer_comment",
		side_effect=Exception("boom"),
	)
	def test_refresh_never_raises(self, mock_fetch):
		from ecommerce_integrations.shopware6.order.order_sync import refresh_customer_comment

		self.assertFalse(refresh_customer_comment("SO-00001", "order-id", {"data": {"isUpdate": True}}))


class TestDocumentHooks(ShopwareTestCase):
	"""Hook behaviour against real doctype meta (fields installed in setUpClass)."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cc.setup_custom_fields()

	def test_custom_fields_installed(self):
		for doctype in ("Sales Order", "Delivery Note", "Pick List", "Packing Slip"):
			meta = frappe.get_meta(doctype)
			self.assertTrue(meta.has_field(cc.COMMENT_FIELD), doctype)
			self.assertTrue(meta.has_field(cc.HIGHLIGHT_FIELD), doctype)
		so_field = frappe.get_meta("Sales Order").get_field(cc.COMMENT_FIELD)
		self.assertEqual(so_field.allow_on_submit, 1)
		dn_field = frappe.get_meta("Delivery Note").get_field(cc.COMMENT_FIELD)
		self.assertEqual(dn_field.read_only, 1)

	def test_set_customer_comment_sets_flag(self):
		so = frappe.new_doc("Sales Order")
		with patch.object(cc.frappe, "conf", frappe._dict()):
			self.assertTrue(cc.set_customer_comment(so, f"<p>{PICKUP_COMMENT}</p>"))
		self.assertEqual(so.get(cc.COMMENT_FIELD), PICKUP_COMMENT)
		self.assertEqual(so.get(cc.HIGHLIGHT_FIELD), 1)

	def test_set_customer_comment_ignores_empty(self):
		so = frappe.new_doc("Sales Order")
		self.assertFalse(cc.set_customer_comment(so, "   "))
		self.assertFalse(so.get(cc.COMMENT_FIELD))

	def test_sales_order_validate_normalises_manual_input(self):
		so = frappe.new_doc("Sales Order")
		so.set(cc.COMMENT_FIELD, "  abholung: Messe <b>Halle 3</b> ")
		with patch.object(cc.frappe, "conf", frappe._dict()):
			cc.sales_order_validate(so)
		self.assertEqual(so.get(cc.COMMENT_FIELD), "abholung: Messe Halle 3")
		self.assertEqual(so.get(cc.HIGHLIGHT_FIELD), 1)

		so.set(cc.COMMENT_FIELD, "")
		cc.sales_order_validate(so)
		self.assertEqual(so.get(cc.HIGHLIGHT_FIELD), 0)

	def test_pick_list_combines_orders(self):
		pl = frappe.new_doc("Pick List")
		pl.append("locations", {"sales_order": "SO-00001"})
		pl.append("locations", {"sales_order": "SO-00002"})
		pl.append("locations", {"sales_order": "SO-00001"})
		rows = [
			frappe._dict({"name": "SO-00001", cc.COMMENT_FIELD: "Tor 2", cc.HIGHLIGHT_FIELD: 0}),
			frappe._dict({"name": "SO-00002", cc.COMMENT_FIELD: PICKUP_COMMENT, cc.HIGHLIGHT_FIELD: 1}),
		]
		with patch.object(cc.frappe, "get_all", return_value=rows):
			cc.propagate_to_pick_list(pl)
		self.assertEqual(pl.get(cc.COMMENT_FIELD), f"[SO-00001] Tor 2\n\n[SO-00002] {PICKUP_COMMENT}")
		self.assertEqual(pl.get(cc.HIGHLIGHT_FIELD), 1)

	def test_delivery_note_single_order(self):
		dn = frappe.new_doc("Delivery Note")
		dn.append("items", {"against_sales_order": "SO-00001"})
		rows = [frappe._dict({"name": "SO-00001", cc.COMMENT_FIELD: PICKUP_COMMENT, cc.HIGHLIGHT_FIELD: 1})]
		with patch.object(cc.frappe, "get_all", return_value=rows):
			cc.propagate_to_delivery_note(dn)
		self.assertEqual(dn.get(cc.COMMENT_FIELD), PICKUP_COMMENT)
		self.assertEqual(dn.get(cc.HIGHLIGHT_FIELD), 1)

	def test_delivery_note_without_order_untouched(self):
		dn = frappe.new_doc("Delivery Note")
		dn.append("items", {"item_code": "ITEM-001"})
		get_all = MagicMock()
		with patch.object(cc.frappe, "get_all", get_all):
			cc.propagate_to_delivery_note(dn)
		get_all.assert_not_called()
		self.assertFalse(dn.get(cc.COMMENT_FIELD))
