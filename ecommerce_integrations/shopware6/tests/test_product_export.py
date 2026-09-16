"""Tests for pure helpers in ``shopware6.export.utils``.

Pure functions are tested directly; anything that needs a Shopware
Setting goes through small mocks so the suite runs on any clean site.
"""

import unittest


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


if __name__ == "__main__":
	unittest.main()
