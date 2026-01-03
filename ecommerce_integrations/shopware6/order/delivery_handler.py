"""
Shopware 6 Delivery Handler

Handles Delivery Note creation from Shopware shipment data.
"""

from typing import Any, Dict, Optional

import frappe


def create_delivery_note(
    sales_order: str,
    delivery_data: Dict[str, Any],
    setting
) -> Optional[str]:
    """
    Create Delivery Note from Shopware shipment data.

    Args:
        sales_order: ERPNext Sales Order name
        delivery_data: Shopware delivery object
        setting: Shopware Setting document

    Returns:
        Delivery Note name if created
    """
    try:
        # Check if DN already exists
        existing = frappe.db.get_value(
            "Delivery Note", {"shopware_delivery_id": delivery_data.get("id")}, "name"
        )
        if existing:
            return existing

        # Create Delivery Note from Sales Order
        from erpnext.selling.doctype.sales_order.sales_order import make_delivery_note

        dn = make_delivery_note(sales_order)
        dn.naming_series = setting.delivery_note_series
        dn.shopware_delivery_id = delivery_data.get("id")

        # Set tracking number if available
        tracking = delivery_data.get("trackingCodes", [])
        if tracking:
            dn.lr_no = tracking[0] if isinstance(tracking, list) else tracking

        dn.insert(ignore_permissions=True)
        dn.submit()

        return dn.name

    except Exception as e:
        get_logger().error("Failed to create Delivery Note for {sales_order}", persist=False)
        return None


def get_shipping_address_from_delivery(delivery_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Extract shipping address from Shopware delivery object.

    Args:
        delivery_data: Shopware delivery object

    Returns:
        Address dict with ERPNext fields
    """
    shipping_address = delivery_data.get("shippingOrderAddress", {}) or {}
    country = shipping_address.get("country", {}) or {}
    country_state = shipping_address.get("countryState", {}) or {}

    return {
        "address_line1": shipping_address.get("street", ""),
        "address_line2": shipping_address.get("additionalAddressLine1", ""),
        "city": shipping_address.get("city", ""),
        "pincode": shipping_address.get("zipcode", ""),
        "country": country.get("name", ""),
        "state": country_state.get("name", ""),
    }


def get_shipping_method_name(delivery_data: Dict[str, Any]) -> Optional[str]:
    """
    Get shipping method name from delivery data.

    Args:
        delivery_data: Shopware delivery object

    Returns:
        Shipping method name if available
    """
    shipping_method = delivery_data.get("shippingMethod", {}) or {}
    return shipping_method.get("name")
