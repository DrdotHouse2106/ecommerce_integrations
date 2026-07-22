"""
API endpoints for Shopware frontend to fetch quotations.
"""
import frappe
from frappe import _


@frappe.whitelist(allow_guest=False)
def get_quotations_by_shopware_customer(shopware_customer_id: str):
    """
    Get all quotations for a customer identified by their Shopware Customer ID.

    Args:
        shopware_customer_id: The Shopware customer UUID

    Returns:
        List of quotations with relevant fields
    """
    if not shopware_customer_id:
        frappe.throw(_("Shopware Customer ID ist erforderlich"))

    # Find ERPNext Customer by shopware_customer_id
    customer_name = frappe.db.get_value(
        "Customer",
        {"shopware_customer_id": shopware_customer_id},
        "name"
    )

    if not customer_name:
        return []

    # SECURITY: Permission check — without this, any authenticated user
    # who knows a Shopware customer UUID can enumerate that customer's
    # quotations. Enforce that the caller may read the underlying Customer
    # doc; Quotation rows are further filtered by Frappe's permission
    # query layer through ``ignore_permissions=False`` (the default).
    if not frappe.has_permission("Customer", "read", doc=customer_name):
        frappe.throw(
            _("Keine Berechtigung, Angebote für diesen Kunden zu lesen"),
            frappe.PermissionError,
        )

    # Get quotations for this customer (party_name is the field in Quotation)
    quotations = frappe.get_all(
        "Quotation",
        filters={"party_name": customer_name, "quotation_to": "Customer"},
        fields=["name", "transaction_date", "valid_till", "grand_total", "status", "currency"],
        order_by="transaction_date desc",
        limit_page_length=50,
        ignore_permissions=False,
    )

    return quotations
