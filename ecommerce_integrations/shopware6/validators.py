"""
Shopware 6 Data Validators

Input validation for Shopware data to ensure data integrity
and provide clear error messages.
"""

from typing import Any, ClassVar

import frappe

from ecommerce_integrations.shopware6.utils import get_logger


class ValidationError(Exception):
    """Custom exception for validation errors."""
    pass


class ShopwareDataValidator:
    """
    Validator for Shopware API data.

    Validates incoming data from Shopware webhooks and API responses
    to ensure required fields are present and have valid values.
    """

    # Required fields for different entity types
    ORDER_REQUIRED_FIELDS: ClassVar[list[str]] = ["id", "orderNumber", "orderCustomer"]
    CUSTOMER_REQUIRED_FIELDS: ClassVar[list[str]] = ["id", "email"]
    PRODUCT_REQUIRED_FIELDS: ClassVar[list[str]] = ["id", "productNumber"]
    CATEGORY_REQUIRED_FIELDS: ClassVar[list[str]] = ["id", "name"]
    WEBHOOK_REQUIRED_FIELDS: ClassVar[list[str]] = ["source", "data"]

    @classmethod
    def validate_order(cls, order_data: dict[str, Any]) -> tuple[bool, list[str]]:
        """
        Validate Shopware order data.

        Args:
            order_data: Shopware order object

        Returns:
            Tuple of (is_valid, list of error messages)
        """
        errors = []

        if not order_data:
            return False, ["Order data is empty"]

        # Check required fields
        for field in cls.ORDER_REQUIRED_FIELDS:
            if field not in order_data or order_data[field] is None:
                errors.append(f"Missing required field: {field}")

        # Validate order ID format (UUID)
        order_id = order_data.get("id", "")
        if order_id and not cls._is_valid_uuid(order_id):
            errors.append(f"Invalid order ID format: {order_id}")

        # Validate order number
        order_number = order_data.get("orderNumber", "")
        if not order_number:
            errors.append("Order number is empty")

        # Validate order customer
        order_customer = order_data.get("orderCustomer", {})
        if not order_customer:
            errors.append("Order customer data is missing")
        elif not order_customer.get("email"):
            errors.append("Order customer email is missing")

        # Validate line items (optional but log warning if empty)
        line_items = order_data.get("lineItems", [])
        if not line_items:
            frappe.logger("shopware6").warning(
                f"Order {order_number} has no line items"
            )

        return len(errors) == 0, errors

    @classmethod
    def validate_customer(cls, customer_data: dict[str, Any]) -> tuple[bool, list[str]]:
        """
        Validate Shopware customer data.

        Args:
            customer_data: Shopware customer object

        Returns:
            Tuple of (is_valid, list of error messages)
        """
        errors = []

        if not customer_data:
            return False, ["Customer data is empty"]

        # Check required fields
        for field in cls.CUSTOMER_REQUIRED_FIELDS:
            if field not in customer_data or customer_data[field] is None:
                errors.append(f"Missing required field: {field}")

        # Validate email format
        email = customer_data.get("email", "")
        if email and not cls._is_valid_email(email):
            errors.append(f"Invalid email format: {email}")

        # Validate customer ID format (UUID)
        customer_id = customer_data.get("id", "")
        if customer_id and not cls._is_valid_uuid(customer_id):
            errors.append(f"Invalid customer ID format: {customer_id}")

        return len(errors) == 0, errors

    @classmethod
    def validate_product(cls, product_data: dict[str, Any]) -> tuple[bool, list[str]]:
        """
        Validate Shopware product data.

        Args:
            product_data: Shopware product object

        Returns:
            Tuple of (is_valid, list of error messages)
        """
        errors = []

        if not product_data:
            return False, ["Product data is empty"]

        # Check required fields
        for field in cls.PRODUCT_REQUIRED_FIELDS:
            if field not in product_data or product_data[field] is None:
                errors.append(f"Missing required field: {field}")

        # Validate product ID format (UUID)
        product_id = product_data.get("id", "")
        if product_id and not cls._is_valid_uuid(product_id):
            errors.append(f"Invalid product ID format: {product_id}")

        # Validate product number
        product_number = product_data.get("productNumber", "")
        if not product_number:
            errors.append("Product number is empty")

        # Validate price if present
        price = product_data.get("price", [])
        if price and isinstance(price, list) and len(price) > 0:
            price_entry = price[0]
            if price_entry.get("gross") is None and price_entry.get("net") is None:
                errors.append("Product price has no gross or net value")

        return len(errors) == 0, errors

    @classmethod
    def validate_category(cls, category_data: dict[str, Any]) -> tuple[bool, list[str]]:
        """
        Validate Shopware category data.

        Args:
            category_data: Shopware category object

        Returns:
            Tuple of (is_valid, list of error messages)
        """
        errors = []

        if not category_data:
            return False, ["Category data is empty"]

        # Check required fields
        for field in cls.CATEGORY_REQUIRED_FIELDS:
            if field not in category_data or category_data[field] is None:
                errors.append(f"Missing required field: {field}")

        # Validate category ID format (UUID)
        category_id = category_data.get("id", "")
        if category_id and not cls._is_valid_uuid(category_id):
            errors.append(f"Invalid category ID format: {category_id}")

        return len(errors) == 0, errors

    @classmethod
    def validate_webhook_payload(cls, payload: dict[str, Any]) -> tuple[bool, list[str]]:
        """
        Validate Shopware webhook payload.

        Args:
            payload: Webhook payload

        Returns:
            Tuple of (is_valid, list of error messages)
        """
        errors = []

        if not payload:
            return False, ["Webhook payload is empty"]

        # Check for entity ID (can be in multiple locations)
        entity_id = (
            payload.get("primaryKey") or
            payload.get("id") or
            payload.get("payload", {}).get("entity", {}).get("id") or
            payload.get("data", {}).get("orderId") or
            payload.get("data", {}).get("customerId") or
            payload.get("data", {}).get("productId")
        )

        if not entity_id:
            errors.append("No entity ID found in webhook payload")

        return len(errors) == 0, errors

    @classmethod
    def validate_line_item(cls, line_item: dict[str, Any]) -> tuple[bool, list[str]]:
        """
        Validate Shopware order line item.

        Args:
            line_item: Shopware line item object

        Returns:
            Tuple of (is_valid, list of error messages)
        """
        errors = []

        if not line_item:
            return False, ["Line item data is empty"]

        # Check for product type items
        item_type = line_item.get("type", "")
        if item_type == "product":
            # Product items should have a productId or referencedId
            if not line_item.get("productId") and not line_item.get("referencedId"):
                errors.append("Product line item has no productId or referencedId")

            # Should have quantity
            quantity = line_item.get("quantity")
            if quantity is None or quantity <= 0:
                errors.append("Invalid quantity for line item")

        return len(errors) == 0, errors

    @staticmethod
    def _is_valid_uuid(value: str) -> bool:
        """Check if a string is a valid UUID format."""
        import re
        uuid_pattern = re.compile(
            r'^[a-f0-9]{32}$|^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$',
            re.IGNORECASE
        )
        return bool(uuid_pattern.match(value))

    @staticmethod
    def _is_valid_email(email: str) -> bool:
        """Check if a string is a valid email format."""
        import re
        email_pattern = re.compile(
            r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
        )
        return bool(email_pattern.match(email))


class ERPNextDataValidator:
    """
    Validator for ERPNext data before syncing to Shopware.
    """

    @classmethod
    def validate_item_for_export(cls, item_code: str) -> tuple[bool, list[str]]:
        """
        Validate an ERPNext Item before exporting to Shopware.

        Args:
            item_code: ERPNext Item code

        Returns:
            Tuple of (is_valid, list of error messages)
        """
        errors = []

        if not frappe.db.exists("Item", item_code):
            return False, [f"Item {item_code} does not exist"]

        item = frappe.get_doc("Item", item_code)

        # Check if item is disabled
        if item.disabled:
            errors.append(f"Item {item_code} is disabled")

        # Check if item has a name (required for Shopware)
        if not item.item_name:
            errors.append(f"Item {item_code} has no item_name")

        # Check if item has a description (highly recommended)
        if not item.description:
            frappe.logger("shopware6").warning(
                f"Item {item_code} has no description, will use item_name"
            )

        # Validate price for simple products (not templates)
        # Skip price validation for discount items (configured in Shopware Settings)
        if not item.has_variants:
            is_discount_item = cls._is_discount_item(item_code)
            if not is_discount_item and not cls._item_has_price(item_code):
                errors.append(f"Item {item_code} has no selling price - cannot sync to Shopware")

        return len(errors) == 0, errors

    @classmethod
    def validate_customer_for_export(cls, customer_name: str) -> tuple[bool, list[str]]:
        """
        Validate an ERPNext Customer before exporting to Shopware.

        Args:
            customer_name: ERPNext Customer name

        Returns:
            Tuple of (is_valid, list of error messages)
        """
        errors = []

        if not frappe.db.exists("Customer", customer_name):
            return False, [f"Customer {customer_name} does not exist"]

        customer = frappe.get_doc("Customer", customer_name)

        # Check if customer is disabled
        if customer.disabled:
            errors.append(f"Customer {customer_name} is disabled")

        return len(errors) == 0, errors

    @staticmethod
    def _item_has_price(item_code: str) -> bool:
        """Check if an item has a selling price."""
        # Check standard_rate first
        standard_rate = frappe.db.get_value("Item", item_code, "standard_rate")
        if standard_rate and float(standard_rate) > 0:
            return True

        # Check Item Price table
        item_price = frappe.db.get_value(
            "Item Price",
            {"item_code": item_code, "selling": 1},
            "price_list_rate"
        )
        return bool(item_price and float(item_price) > 0)

    @staticmethod
    def _is_discount_item(item_code: str) -> bool:
        """Check if item is the configured discount item (should be skipped for price validation)."""
        from ecommerce_integrations.shopware6.constants import SETTING_DOCTYPE

        try:
            setting = frappe.get_cached_doc(SETTING_DOCTYPE)
            discount_item = getattr(setting, "discount_item", None)
            return bool(discount_item and item_code == discount_item)
        except Exception:
            return False


def validate_and_log(
    validator_func,
    data: dict[str, Any],
    entity_type: str,
    entity_id: str | None = None
) -> bool:
    """
    Validate data and log any errors.

    Args:
        validator_func: Validation function to call
        data: Data to validate
        entity_type: Type of entity (order, customer, product, etc.)
        entity_id: Optional entity ID for logging

    Returns:
        True if valid, False otherwise
    """
    is_valid, errors = validator_func(data)

    if not is_valid:
        error_msg = f"Validation failed for {entity_type}"
        if entity_id:
            error_msg += f" ({entity_id})"
        error_msg += f": {', '.join(errors)}"

        get_logger("validate_and_log").error(error_msg, persist=False)

    return is_valid


class DestructiveOperationResult:
    """Result of destructive operation validation."""

    def __init__(self, proceed: bool, message: str, entity_count: int = 0):
        self.proceed = proceed
        self.message = message
        self.entity_count = entity_count

    def __bool__(self):
        return self.proceed


def validate_before_destructive_operation(
    operation: str,
    entity_count: int,
    threshold: int = 50,
    force: bool = False
) -> DestructiveOperationResult:
    """
    Validate before performing a destructive operation.

    Prevents accidental deletion of large numbers of entities by requiring
    explicit confirmation (force=True) when the affected count exceeds a threshold.

    Args:
        operation: Description of the operation (e.g., "delete_product_prices")
        entity_count: Number of entities that will be affected
        threshold: Maximum entities to affect without force=True (default: 50)
        force: If True, bypass the threshold check

    Returns:
        DestructiveOperationResult with proceed flag and message

    Usage:
        result = validate_before_destructive_operation(
            "delete_all_prices",
            entity_count=len(prices_to_delete),
            threshold=10,
            force=False
        )
        if not result.proceed:
            return {"success": False, "message": result.message}
    """
    if force:
        return DestructiveOperationResult(
            proceed=True,
            message=f"Force mode: proceeding with {operation} affecting {entity_count} entities",
            entity_count=entity_count
        )

    if entity_count > threshold:
        return DestructiveOperationResult(
            proceed=False,
            message=(
                f"Operation '{operation}' would affect {entity_count} entities. "
                f"This exceeds the safety threshold of {threshold}. "
                f"Use force=True to proceed anyway."
            ),
            entity_count=entity_count
        )

    return DestructiveOperationResult(
        proceed=True,
        message=f"Proceeding with {operation} affecting {entity_count} entities",
        entity_count=entity_count
    )


def validate_bulk_delete(
    items_to_delete: list[Any],
    item_type: str,
    threshold: int = 50,
    force: bool = False
) -> DestructiveOperationResult:
    """
    Validate before bulk deleting items.

    Convenience wrapper around validate_before_destructive_operation for delete operations.

    Args:
        items_to_delete: List of items that will be deleted
        item_type: Type of items being deleted (e.g., "prices", "categories", "media")
        threshold: Maximum items to delete without force=True
        force: If True, bypass the threshold check

    Returns:
        DestructiveOperationResult with proceed flag and message
    """
    return validate_before_destructive_operation(
        operation=f"delete_{item_type}",
        entity_count=len(items_to_delete),
        threshold=threshold,
        force=force
    )
