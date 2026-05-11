"""Tests for ShopwareProductUploader + pure helpers in
``shopware6.export.utils`` and ``shopware6.export.image_handler``.

Pure functions are tested directly; anything that needs a Shopware
Setting goes through small mocks so the suite runs on any clean site.
"""

import os
import tempfile
import unittest
from unittest.mock import patch, MagicMock

from ecommerce_integrations.shopware6.tests.utils import ShopwareTestCase


class TestShopwareProductUploader(ShopwareTestCase):
	"""Construction-time behaviour. Full upload is integration-only."""

	@patch("ecommerce_integrations.shopware6.export.product_uploader.frappe")
	@patch("ecommerce_integrations.shopware6.export.product_uploader.get_shopware_document_id")
	def test_initialization_stores_item_code(self, mock_get_doc_id, mock_frappe):
		from ecommerce_integrations.shopware6.export.product_uploader import (
			ShopwareProductUploader,
		)
		mock_get_doc_id.return_value = None
		mock_frappe.get_cached_doc.return_value = MagicMock()
		uploader = ShopwareProductUploader(item_code="TEST-001")
		self.assertEqual(uploader.item_code, "TEST-001")
		self.assertIsNone(uploader.shopware_id)

	@patch("ecommerce_integrations.shopware6.export.product_uploader.frappe")
	@patch("ecommerce_integrations.shopware6.export.product_uploader.get_shopware_document_id")
	def test_is_synced_false_when_no_mapping(self, mock_get_doc_id, mock_frappe):
		from ecommerce_integrations.shopware6.export.product_uploader import (
			ShopwareProductUploader,
		)
		mock_get_doc_id.return_value = None
		mock_frappe.get_cached_doc.return_value = MagicMock()
		uploader = ShopwareProductUploader(item_code="NEW-ITEM")
		self.assertFalse(uploader.is_synced())

	@patch("ecommerce_integrations.shopware6.export.product_uploader.frappe")
	@patch("ecommerce_integrations.shopware6.export.product_uploader.get_shopware_document_id")
	def test_is_synced_true_when_mapping_exists(self, mock_get_doc_id, mock_frappe):
		from ecommerce_integrations.shopware6.export.product_uploader import (
			ShopwareProductUploader,
		)
		mock_get_doc_id.return_value = "existing-shopware-id"
		mock_frappe.get_cached_doc.return_value = MagicMock()
		uploader = ShopwareProductUploader(item_code="SYNCED-ITEM")
		self.assertTrue(uploader.is_synced())


class TestExportUtils(unittest.TestCase):
	"""Pure helpers from ``shopware6.export.utils``.

	These are deterministic, no Frappe state required.
	"""

	def test_generate_uuid_deterministic(self):
		from ecommerce_integrations.shopware6.export.utils import generate_uuid

		# Same input → same output (idempotent).
		self.assertEqual(generate_uuid("ITEM-001"), generate_uuid("ITEM-001"))
		# Different inputs → different outputs.
		self.assertNotEqual(generate_uuid("ITEM-001"), generate_uuid("ITEM-002"))

	def test_generate_uuid_md5_shape(self):
		from ecommerce_integrations.shopware6.export.utils import generate_uuid

		# 32 hex chars (MD5).
		result = generate_uuid("anything")
		self.assertEqual(len(result), 32)
		self.assertTrue(all(c in "0123456789abcdef" for c in result))

	def test_sanitize_filename_replaces_illegal_chars(self):
		from ecommerce_integrations.shopware6.export.utils import sanitize_filename

		# Shopware rejects | < > : " / \ ? * in filenames.
		self.assertEqual(sanitize_filename('file|name.jpg'), "file_name.jpg")
		self.assertEqual(sanitize_filename('a/b\\c:d.jpg'), "a_b_c_d.jpg")
		self.assertEqual(sanitize_filename('q?<>".jpg'), "q____.jpg")

	def test_sanitize_filename_keeps_safe_chars(self):
		from ecommerce_integrations.shopware6.export.utils import sanitize_filename

		# Spaces, dots, dashes, underscores, unicode — all kept as-is.
		self.assertEqual(sanitize_filename("My-File 2024.jpg"), "My-File 2024.jpg")
		self.assertEqual(sanitize_filename("Größe_42.png"), "Größe_42.png")


class TestImageHandlerHelpers(unittest.TestCase):
	"""Pure helpers from ``shopware6.export.image_handler``."""

	def test_get_file_hash_returns_md5_for_local_file(self):
		from ecommerce_integrations.shopware6.export import image_handler

		# ``get_file_hash`` resolves ``/files/<name>`` via ``get_files_path()``.
		# Redirect that to a TemporaryDirectory so the test leaves no artifacts
		# under the live site and stays independent of site state.
		with tempfile.TemporaryDirectory() as tmp:
			path = os.path.join(tmp, "image.jpg")
			with open(path, "wb") as f:
				f.write(b"hello-image-content")
			with patch.object(image_handler, "get_files_path", return_value=tmp):
				h1 = image_handler.get_file_hash("image.jpg")
				h2 = image_handler.get_file_hash("image.jpg")
		self.assertIsNotNone(h1)
		self.assertEqual(h1, h2)
		self.assertEqual(len(h1), 32)

	def test_get_file_hash_returns_none_for_missing_file(self):
		from ecommerce_integrations.shopware6.export.image_handler import get_file_hash

		self.assertIsNone(get_file_hash("/files/does-not-exist-9f8e7d6c5b4a.jpg"))

	def test_get_file_hash_returns_none_for_http_urls(self):
		# Network URLs are intentionally not hashed (would require a request);
		# the caller falls back to a fetch + md5 elsewhere.
		from ecommerce_integrations.shopware6.export.image_handler import get_file_hash

		self.assertIsNone(get_file_hash("https://example.com/image.jpg"))

	def test_is_safe_url_blocks_loopback(self):
		from ecommerce_integrations.shopware6.export.image_handler import _is_safe_url

		# SSRF protection: 127.0.0.1, localhost, RFC1918 ranges should never be fetched.
		self.assertFalse(_is_safe_url("http://127.0.0.1/x.jpg"))
		self.assertFalse(_is_safe_url("http://localhost/x.jpg"))

	def test_is_safe_url_blocks_private_ranges(self):
		from ecommerce_integrations.shopware6.export.image_handler import _is_safe_url

		self.assertFalse(_is_safe_url("http://10.0.0.1/x.jpg"))
		self.assertFalse(_is_safe_url("http://192.168.1.1/x.jpg"))

	def test_is_safe_url_allows_public_urls(self):
		from ecommerce_integrations.shopware6.export.image_handler import _is_safe_url

		# 8.8.8.8 (Google DNS) is public and parses without a real DNS lookup:
		# ``ipaddress.ip_address(socket.gethostbyname("8.8.8.8"))`` short-circuits
		# the dotted-quad rather than issuing a network call.
		self.assertTrue(_is_safe_url("https://8.8.8.8/x.jpg"))


if __name__ == "__main__":
	unittest.main()
