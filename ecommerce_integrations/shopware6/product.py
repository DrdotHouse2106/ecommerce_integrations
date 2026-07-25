"""
Shopware 6 Product Sync Module

Handles synchronization of products between Shopware 6 and ERPNext.
Uses lib_shopware6_api_base SDK for API communication.
"""

from typing import Any

import frappe
from frappe import _
from frappe.utils import cstr, flt
from frappe.utils.nestedset import get_root_of
from lib_shopware6_api_base import (
    Criteria,
    EqualsFilter,
    Shopware6AdminAPIClientBase,
)

from ecommerce_integrations.ecommerce_integrations.doctype.ecommerce_item import ecommerce_item
from ecommerce_integrations.shopware6.connection import get_shopware_client
from ecommerce_integrations.shopware6.constants import (
    ITEM_SELLING_RATE_FIELD,
    MODULE_NAME,
    SETTING_DOCTYPE,
    WEIGHT_TO_ERPNEXT_UOM_MAP,
)
from ecommerce_integrations.shopware6.utils import (
    get_logger,
    get_price_from_shopware_price_object,
    get_tax_rate_from_shopware_product,
    update_shopware_log,
)


class ShopwareProduct:
    """
    Handles Shopware product synchronization to ERPNext.

    Shopware 6 product structure:
    - Products can be simple or have variants (children)
    - Variants are linked via parentId
    - Options/Properties define variant attributes
    - Prices are stored in a nested structure with currency support
    """

    def __init__(
        self,
        product_id: str,
        variant_id: str | None = None,
        sku: str | None = None,
        has_variants: int = 0,
    ):
        self.product_id = str(product_id) if product_id else None
        self.variant_id = str(variant_id) if variant_id else None
        self.sku = str(sku) if sku else None
        self.has_variants = has_variants
        self.setting = frappe.get_doc(SETTING_DOCTYPE)

        if not self.setting.is_enabled():
            frappe.throw(_("Shopware-Produkt kann nicht synchronisiert werden, solange die Integration deaktiviert ist."))

    def is_synced(self) -> bool:
        """Check if product is already synced to ERPNext."""
        return ecommerce_item.is_synced(
            MODULE_NAME,
            integration_item_code=self.product_id,
            variant_id=self.variant_id,
            sku=self.sku,
        )

    def get_erpnext_item(self):
        """Get the linked ERPNext Item if synced."""
        return ecommerce_item.get_erpnext_item(
            MODULE_NAME,
            integration_item_code=self.product_id,
            variant_id=self.variant_id,
            sku=self.sku,
            has_variants=self.has_variants,
        )

    def sync_product(self):
        """
        Sync product from Shopware to ERPNext.

        Fetches product data from Shopware API and creates/updates ERPNext Item.
        """
        if self.is_synced():
            return

        # Get client - don't use decorator on instance methods
        client = get_shopware_client()

        # Fetch product with associations (including tax for accurate price calculations)
        criteria = Criteria()
        criteria.associations["prices"] = Criteria()
        criteria.associations["options"] = Criteria()
        criteria.associations["options"].associations["group"] = Criteria()
        criteria.associations["children"] = Criteria()
        criteria.associations["children"].associations["prices"] = Criteria()
        criteria.associations["children"].associations["options"] = Criteria()
        criteria.associations["children"].associations["tax"] = Criteria()
        criteria.associations["manufacturer"] = Criteria()
        criteria.associations["media"] = Criteria()
        criteria.associations["cover"] = Criteria()
        criteria.associations["tax"] = Criteria()  # For accurate net/gross conversion

        response = client.request_post(
            "search/product",
            Criteria(ids=[self.product_id])
        )

        products = response.data or []
        if not products:
            frappe.throw(_("Produkt nicht in Shopware gefunden: {0}").format(self.product_id))

        product_data = products[0]
        self._make_item(product_data, client)

    def _make_item(self, product_data: dict[str, Any], client: Shopware6AdminAPIClientBase):
        """Create ERPNext Item from Shopware product data."""
        warehouse = self.setting.warehouse

        # Check if product has variants (children)
        children = product_data.get("children", [])
        if children and len(children) > 0:
            self.has_variants = 1
            attributes = self._create_attributes(product_data)
            self._create_item(product_data, warehouse, has_variant=1, attributes=attributes)
            self._create_item_variants(product_data, warehouse, attributes, client)
        else:
            self._create_item(product_data, warehouse)

    def _create_attributes(self, product_data: dict[str, Any]) -> list[dict]:
        """
        Create/update Item Attributes from Shopware product options.

        Shopware stores variant options in property groups.
        """
        attributes = []
        options = product_data.get("options", []) or []

        # Group options by their group
        option_groups = {}
        for option in options:
            group = option.get("group", {})
            group_name = group.get("name", "")
            if group_name:
                if group_name not in option_groups:
                    option_groups[group_name] = []
                option_groups[group_name].append(option.get("name", ""))

        for group_name, values in option_groups.items():
            if not frappe.db.get_value("Item Attribute", group_name, "name"):
                # Create new attribute
                frappe.get_doc({
                    "doctype": "Item Attribute",
                    "attribute_name": group_name,
                    "item_attribute_values": [
                        {"attribute_value": v, "abbr": v[:10]}
                        for v in values
                    ],
                }).insert()
            else:
                # Update existing attribute with new values
                item_attr = frappe.get_doc("Item Attribute", group_name)
                if not item_attr.numeric_values:
                    existing_values = [d.attribute_value.lower() for d in item_attr.item_attribute_values]
                    for value in values:
                        if value.lower() not in existing_values:
                            item_attr.append("item_attribute_values", {
                                "attribute_value": value,
                                "abbr": value[:10],
                            })
                    item_attr.save()

            attributes.append({"attribute": group_name})

        return attributes

    def _create_item(
        self,
        product_data: dict[str, Any],
        warehouse: str,
        has_variant: int = 0,
        attributes: list | None = None,
        variant_of: str | None = None,
    ):
        """Create ERPNext Item from Shopware product data.

        Supports both B2B (net prices) and B2C (gross prices) modes.
        Uses the actual tax rate from the Shopware product for accurate conversion.
        """
        # Extract basic info
        product_number = product_data.get("productNumber", "")
        name = product_data.get("name", "") or (product_data.get("translated") or {}).get("name", "")
        description = product_data.get("description", "") or (product_data.get("translated") or {}).get("description", "")

        # Get actual tax rate from Shopware product (instead of assuming 19%)
        tax_rate = get_tax_rate_from_shopware_product(product_data)

        # Get price - handle B2B (net) vs B2C (gross) mode
        # Use the actual tax rate for accurate net/gross conversion
        prices = product_data.get("prices", []) or product_data.get("price", [])
        use_net_prices = getattr(self.setting, 'import_prices_as_net', True)

        if use_net_prices:
            # B2B mode: Get net price, using actual tax rate for conversion if needed
            price = get_price_from_shopware_price_object(prices, return_net=True, tax_rate=tax_rate)
        else:
            # B2C mode: Use gross price
            price = get_price_from_shopware_price_object(prices, return_net=False, tax_rate=tax_rate)

        # Get weight
        weight = flt(product_data.get("weight", 0))
        weight_unit = "kg"  # Shopware default is kg

        # Get image URL
        image_url = None
        cover = product_data.get("cover", {})
        if cover:
            media = cover.get("media", {})
            if media:
                image_url = media.get("url")

        # Get manufacturer as supplier
        manufacturer = product_data.get("manufacturer", {})
        supplier_name = manufacturer.get("name", "") if manufacturer else ""

        item_dict = {
            "variant_of": variant_of,
            "is_stock_item": 1,
            "item_code": product_number or cstr(product_data.get("id", ""))[:20],
            "item_name": name.strip() if name else product_number,
            "description": description or name,
            "item_group": self._get_item_group(product_data),
            "has_variants": has_variant,
            "attributes": attributes or [],
            # Literal "Nos"/"Kg", not _(...) — these are Link-field
            # values that must match tabUOM's actual document name,
            # not a translated display label (e.g. German renders
            # "Nos" as "Stk", which isn't a real UOM record).
            "stock_uom": "Nos",
            "default_warehouse": warehouse,
            "image": image_url,
            "weight_uom": WEIGHT_TO_ERPNEXT_UOM_MAP.get(weight_unit, "Kg"),
            "weight_per_unit": weight,
            "default_supplier": self._get_supplier(supplier_name) if supplier_name else None,
            ITEM_SELLING_RATE_FIELD: price,
        }

        integration_item_code = product_data["id"]
        variant_id = product_data.get("id", "") if has_variant == 0 else ""
        sku = product_number

        # Try to match existing item by SKU
        if not _match_sku_and_link_item(
            item_dict, integration_item_code, variant_id, variant_of=variant_of, has_variant=has_variant
        ):
            ecommerce_item.create_ecommerce_item(
                MODULE_NAME,
                integration_item_code,
                item_dict,
                variant_id=variant_id,
                sku=sku,
                variant_of=variant_of,
                has_variants=has_variant,
            )

    def _create_item_variants(
        self,
        product_data: dict[str, Any],
        warehouse: str,
        attributes: list,
        client: Shopware6AdminAPIClientBase,
    ):
        """Create ERPNext Item variants from Shopware product children."""
        template_item = ecommerce_item.get_erpnext_item(
            MODULE_NAME,
            integration_item_code=product_data.get("id"),
            has_variants=1,
        )

        if not template_item:
            return

        children = product_data.get("children", [])
        for child in children:
            # Get variant options
            child_options = child.get("options", [])

            # Build variant attributes
            variant_attributes = []
            for attr in attributes:
                attr_copy = attr.copy()
                # Find matching option value
                for option in child_options:
                    group = option.get("group", {})
                    if group.get("name") == attr["attribute"]:
                        attr_copy["attribute_value"] = option.get("name", "")
                        break
                variant_attributes.append(attr_copy)

            # TODO: variant pricing is not yet plumbed through to the Item Price
            # record. The parent flow above uses ITEM_SELLING_RATE_FIELD: price
            # computed via get_price_from_shopware_price_object(prices,
            # return_net=use_net_prices, tax_rate=variant_tax_rate) where
            # variant_tax_rate = get_tax_rate_from_shopware_product(child) and
            # prices = child.get("prices") or child.get("price"). Wire that
            # through when variant Item Price creation lands; ruff F841 flagged
            # the computation as dead so it has been removed in the meantime.

            variant_dict = {
                "id": product_data.get("id"),
                "variant_id": child.get("id"),
                "productNumber": child.get("productNumber", ""),
                "name": f"{template_item.item_name}-{child.get('productNumber', '')}",
                "description": child.get("description") or product_data.get("description", ""),
                "weight": child.get("weight", 0),
            }

            self._create_item(
                variant_dict,
                warehouse,
                has_variant=0,
                attributes=variant_attributes,
                variant_of=template_item.name,
            )

    def _get_item_group(self, product_data: dict[str, Any]) -> str:
        """Get or create Item Group from Shopware category."""
        parent_item_group = get_root_of("Item Group")

        # Try to get category from product
        categories = product_data.get("categories", [])
        if categories:
            category_name = categories[0].get("name", "")
            if category_name:
                if frappe.db.get_value("Item Group", category_name, "name"):
                    return category_name

                # Create new item group
                item_group = frappe.get_doc({
                    "doctype": "Item Group",
                    "item_group_name": category_name,
                    "parent_item_group": parent_item_group,
                    "is_group": "No",
                }).insert()
                return item_group.name

        return parent_item_group

    def _get_supplier(self, supplier_name: str) -> str | None:
        """Get or create Supplier from manufacturer name."""
        if not supplier_name:
            return None

        # Check if supplier exists
        existing = frappe.db.get_value(
            "Supplier",
            {"supplier_name": supplier_name},
            "name"
        )
        if existing:
            return existing

        # Create new supplier
        supplier = frappe.get_doc({
            "doctype": "Supplier",
            "supplier_name": supplier_name,
            "supplier_group": self._get_supplier_group(),
        }).insert()
        return supplier.name

    def _get_supplier_group(self) -> str:
        """Get or create Shopware Supplier Group."""
        group_name = _("Shopware Supplier")
        if not frappe.db.get_value("Supplier Group", group_name):
            frappe.get_doc({
                "doctype": "Supplier Group",
                "supplier_group_name": group_name,
            }).insert()
        return group_name


def _match_sku_and_link_item(
    item_dict: dict,
    product_id: str,
    variant_id: str,
    variant_of: str | None = None,
    has_variant: bool = False,
) -> bool:
    """
    Try to match new item with existing item using Shopware SKU == item_code.

    Returns True if matched and linked.
    """
    sku = item_dict.get("item_code", "")
    if not sku or variant_of or has_variant:
        return False

    item_name = frappe.db.get_value("Item", {"item_code": sku})
    if item_name:
        try:
            ecom_item = frappe.get_doc({
                "doctype": "Ecommerce Item",
                "integration": MODULE_NAME,
                "erpnext_item_code": item_name,
                "integration_item_code": product_id,
                "has_variants": 0,
                "variant_id": cstr(variant_id),
                "sku": sku,
            })
            ecom_item.insert()
            return True
        except Exception:
            return False
    return False


def create_items_if_not_exist(order: dict[str, Any]) -> None:
    """
    Sync all items from a Shopware order that aren't already synced.

    Args:
        order: Shopware order data
    """
    logger = get_logger("create_items_if_not_exist")
    line_items = order.get("lineItems") or []

    for item in line_items:
        # Skip non-product items (shipping, discounts, etc.)
        if item.get("type") != "product":
            continue

        product_id = item.get("productId") or item.get("referencedId")
        if not product_id:
            continue

        sku = item.get("payload", {}).get("productNumber", "")

        product = ShopwareProduct(product_id, sku=sku)
        if not product.is_synced():
            try:
                product.sync_product()
            except Exception as e:
                logger.error(
                    f"Failed to sync product {product_id}",
                    exception=e,
                    persist=True
                )


def get_item_code(shopware_item: dict[str, Any]) -> str | None:
    """
    Get ERPNext item code from Shopware line item.

    Args:
        shopware_item: Shopware order line item

    Returns:
        ERPNext item code if found
    """
    product_id = shopware_item.get("productId") or shopware_item.get("referencedId")
    sku = shopware_item.get("payload", {}).get("productNumber", "")

    item = ecommerce_item.get_erpnext_item(
        integration=MODULE_NAME,
        integration_item_code=product_id,
        sku=sku,
    )

    if item:
        return item.item_code
    return None


def sync_product_from_webhook(payload: dict[str, Any], request_id: str | None = None):
    """
    Handle product sync from Shopware webhook.

    Args:
        payload: Webhook payload from Shopware
        request_id: Log entry ID for tracking
    """
    try:
        product_id = payload.get("primaryKey") or payload.get("id")
        if not product_id:
            # Try to extract from entity data
            entity = payload.get("payload", {}).get("entity", {})
            product_id = entity.get("id")

        if product_id:
            product = ShopwareProduct(product_id)
            product.sync_product()

            if request_id:
                update_shopware_log(request_id, status="Success", message=f"Synced product {product_id}")
        else:
            if request_id:
                update_shopware_log(request_id, status="Error", message="No product ID in webhook payload")

    except Exception as e:
        if request_id:
            update_shopware_log(request_id, status="Error", exception=str(e))
        raise


@frappe.whitelist()
def sync_products_from_shopware(limit: int = 100):
    """Manually sync products from Shopware."""
    frappe.only_for("System Manager")
    limit = min(int(limit), 500)
    logger = get_logger("sync_products_from_shopware")
    client = get_shopware_client()

    criteria = Criteria(limit=int(limit))
    criteria.filter.append(EqualsFilter("active", True))
    criteria.associations["prices"] = Criteria()
    criteria.associations["options"] = Criteria()
    criteria.associations["children"] = Criteria()
    criteria.associations["children"].associations["tax"] = Criteria()
    criteria.associations["children"].associations["prices"] = Criteria()
    criteria.associations["manufacturer"] = Criteria()
    criteria.associations["cover"] = Criteria()
    criteria.associations["tax"] = Criteria()  # For accurate net/gross conversion

    response = client.request_post("search/product", criteria)
    products = response.data or []

    logger.info(f"Starting product sync for {len(products)} products")

    synced = 0
    errors = 0

    for product_data in products:
        # Skip child products (variants)
        if product_data.get("parentId"):
            continue

        try:
            product = ShopwareProduct(product_data["id"])
            if not product.is_synced():
                product._make_item(product_data, client)
                synced += 1
        except Exception as e:
            errors += 1
            logger.error(
                f"Failed to sync product {product_data.get('id')}",
                exception=e,
                persist=True
            )

    logger.info(f"Product sync completed: {synced} synced, {errors} errors", persist=True)

    return {
        "synced": synced,
        "errors": errors,
        "total": len(products),
    }
