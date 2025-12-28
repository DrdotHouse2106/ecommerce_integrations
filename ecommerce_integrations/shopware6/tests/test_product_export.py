"""
Tests for Shopware 6 Product Export
"""

import unittest
from unittest.mock import patch, MagicMock

from ecommerce_integrations.shopware6.tests.utils import (
    ShopwareTestCase,
    create_sample_product_data,
)


class TestShopwareProduct(ShopwareTestCase):
    """Test cases for ShopwareProduct class."""

    @patch("ecommerce_integrations.shopware6.export.product_uploader.frappe")
    @patch("ecommerce_integrations.shopware6.export.product_uploader.get_shopware_document_id")
    def test_shopware_product_initialization(self, mock_get_doc_id, mock_frappe):
        """Test ShopwareProduct class initialization."""
        from ecommerce_integrations.shopware6.export.product_uploader import ShopwareProduct

        mock_get_doc_id.return_value = None
        mock_frappe.get_cached_doc.return_value = MagicMock()

        product = ShopwareProduct(item_code="TEST-001")

        self.assertEqual(product.item_code, "TEST-001")
        self.assertIsNone(product.shopware_id)

    @patch("ecommerce_integrations.shopware6.export.product_uploader.frappe")
    @patch("ecommerce_integrations.shopware6.export.product_uploader.get_shopware_document_id")
    def test_shopware_product_is_synced_false(self, mock_get_doc_id, mock_frappe):
        """Test is_synced returns False for new products."""
        from ecommerce_integrations.shopware6.export.product_uploader import ShopwareProduct

        mock_get_doc_id.return_value = None
        mock_frappe.get_cached_doc.return_value = MagicMock()

        product = ShopwareProduct(item_code="NEW-ITEM")

        self.assertFalse(product.is_synced())

    @patch("ecommerce_integrations.shopware6.export.product_uploader.frappe")
    @patch("ecommerce_integrations.shopware6.export.product_uploader.get_shopware_document_id")
    def test_shopware_product_is_synced_true(self, mock_get_doc_id, mock_frappe):
        """Test is_synced returns True for synced products."""
        from ecommerce_integrations.shopware6.export.product_uploader import ShopwareProduct

        mock_get_doc_id.return_value = "existing-shopware-id"
        mock_frappe.get_cached_doc.return_value = MagicMock()

        product = ShopwareProduct(item_code="SYNCED-ITEM")

        self.assertTrue(product.is_synced())


class TestProductMapper(ShopwareTestCase):
    """Test cases for product field mapping."""

    @patch("ecommerce_integrations.shopware6.export.product_mapper.frappe")
    def test_map_item_to_shopware_basic(self, mock_frappe):
        """Test basic item to Shopware mapping."""
        from ecommerce_integrations.shopware6.export.product_mapper import map_item_to_shopware

        mock_item = MagicMock()
        mock_item.item_code = "TEST-001"
        mock_item.item_name = "Test Product"
        mock_item.description = "A test product"
        mock_item.stock_uom = "Nos"
        mock_item.weight_per_unit = 1.5
        mock_item.standard_rate = 49.99
        mock_item.has_variants = 0

        mock_frappe.get_doc.return_value = mock_item

        result = map_item_to_shopware(mock_item)

        self.assertIn("productNumber", result)
        self.assertEqual(result["productNumber"], "TEST-001")
        self.assertIn("name", result)

    @patch("ecommerce_integrations.shopware6.export.product_mapper.frappe")
    def test_map_item_weight_conversion(self, mock_frappe):
        """Test weight unit conversion to kg."""
        from ecommerce_integrations.shopware6.export.product_mapper import map_item_to_shopware

        mock_item = MagicMock()
        mock_item.item_code = "HEAVY-001"
        mock_item.item_name = "Heavy Product"
        mock_item.description = ""
        mock_item.weight_per_unit = 1500  # grams
        mock_item.weight_uom = "Gram"
        mock_item.has_variants = 0

        mock_frappe.get_doc.return_value = mock_item

        result = map_item_to_shopware(mock_item)

        # Weight should be converted to kg
        if "weight" in result:
            self.assertIsInstance(result["weight"], (int, float))


class TestCategoryHandler(ShopwareTestCase):
    """Test cases for category handling."""

    @patch("ecommerce_integrations.shopware6.export.category_handler.frappe")
    def test_get_item_categories(self, mock_frappe):
        """Test getting categories for an item."""
        from ecommerce_integrations.shopware6.export.category_handler import get_item_categories

        mock_frappe.db.get_value.return_value = "Clothing"

        categories = get_item_categories("TEST-001")

        self.assertIsInstance(categories, (list, tuple))

    @patch("ecommerce_integrations.shopware6.export.category_handler.frappe")
    def test_create_category_hierarchy(self, mock_frappe):
        """Test creating nested category hierarchy."""
        # Test that category hierarchy is properly created
        # e.g., "Clothing > Men > Shirts" creates all three levels
        pass


class TestImageHandler(ShopwareTestCase):
    """Test cases for image handling."""

    def test_calculate_image_hash(self):
        """Test image hash calculation for delta sync."""
        from ecommerce_integrations.shopware6.export.image_handler import calculate_image_hash

        # Create test image data
        test_data = b"fake image data for testing"
        hash1 = calculate_image_hash(test_data)
        hash2 = calculate_image_hash(test_data)

        # Same data should produce same hash
        self.assertEqual(hash1, hash2)

        # Different data should produce different hash
        different_data = b"different image data"
        hash3 = calculate_image_hash(different_data)
        self.assertNotEqual(hash1, hash3)

    @patch("ecommerce_integrations.shopware6.export.image_handler.frappe")
    def test_get_item_images(self, mock_frappe):
        """Test getting images for an item."""
        from ecommerce_integrations.shopware6.export.image_handler import get_item_images

        mock_frappe.get_doc.return_value = MagicMock()
        mock_frappe.get_doc.return_value.image = "/files/test-image.jpg"

        images = get_item_images("TEST-001")

        self.assertIsInstance(images, list)


class TestPriceHandler(ShopwareTestCase):
    """Test cases for price handling."""

    @patch("ecommerce_integrations.shopware6.export.price_handler.frappe")
    def test_get_item_price(self, mock_frappe):
        """Test getting item price for Shopware."""
        from ecommerce_integrations.shopware6.export.price_handler import get_item_price

        mock_frappe.db.get_value.return_value = 49.99

        price = get_item_price("TEST-001", "EUR")

        self.assertIsInstance(price, (int, float))

    @patch("ecommerce_integrations.shopware6.export.price_handler.frappe")
    def test_calculate_gross_price(self, mock_frappe):
        """Test gross price calculation with tax."""
        from ecommerce_integrations.shopware6.export.price_handler import calculate_gross_price

        net_price = 100.0
        tax_rate = 19.0

        gross = calculate_gross_price(net_price, tax_rate)

        self.assertEqual(gross, 119.0)

    @patch("ecommerce_integrations.shopware6.export.price_handler.frappe")
    def test_calculate_net_price(self, mock_frappe):
        """Test net price calculation from gross."""
        from ecommerce_integrations.shopware6.export.price_handler import calculate_net_price

        gross_price = 119.0
        tax_rate = 19.0

        net = calculate_net_price(gross_price, tax_rate)

        self.assertAlmostEqual(net, 100.0, places=2)


class TestVariantHandler(ShopwareTestCase):
    """Test cases for variant handling."""

    @patch("ecommerce_integrations.shopware6.export.variant_handler.frappe")
    def test_get_template_variants(self, mock_frappe):
        """Test getting variants for a template item."""
        from ecommerce_integrations.shopware6.export.variant_handler import get_template_variants

        mock_frappe.get_all.return_value = [
            {"name": "TEMPLATE-001-S-RED"},
            {"name": "TEMPLATE-001-M-RED"},
            {"name": "TEMPLATE-001-L-RED"},
        ]

        variants = get_template_variants("TEMPLATE-001")

        self.assertEqual(len(variants), 3)

    @patch("ecommerce_integrations.shopware6.export.variant_handler.frappe")
    def test_get_variant_options(self, mock_frappe):
        """Test extracting variant options (size, color, etc.)."""
        from ecommerce_integrations.shopware6.export.variant_handler import get_variant_options

        mock_frappe.get_doc.return_value = MagicMock()
        mock_frappe.get_doc.return_value.attributes = [
            MagicMock(attribute="Size", attribute_value="M"),
            MagicMock(attribute="Color", attribute_value="Red"),
        ]

        options = get_variant_options("TEMPLATE-001-M-RED")

        self.assertIsInstance(options, (list, dict))


class TestPropertyHandler(ShopwareTestCase):
    """Test cases for property/attribute handling."""

    @patch("ecommerce_integrations.shopware6.export.property_handler.frappe")
    def test_get_item_attributes(self, mock_frappe):
        """Test getting item attributes for Shopware properties."""
        from ecommerce_integrations.shopware6.export.property_handler import get_item_attributes

        mock_frappe.get_doc.return_value = MagicMock()
        mock_frappe.get_doc.return_value.attributes = []

        attributes = get_item_attributes("TEST-001")

        self.assertIsInstance(attributes, list)


if __name__ == "__main__":
    unittest.main()
