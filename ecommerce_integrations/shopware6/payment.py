"""
Shopware 6 Payment Handling Module

Handles payment-related events from Shopware:
- Transaction state changes (for Payment Entry creation)
- Payment status updates
"""

from typing import Any, Dict, Optional

import frappe
from frappe import _
from frappe.utils import flt, nowdate

from ecommerce_integrations.shopware6.constants import (
    SETTING_DOCTYPE,
    PAYMENT_STATE_MAP,
)
from ecommerce_integrations.shopware6.utils import (
    create_shopware_log,
    update_shopware_log,
)


def send_invoice_email(sales_invoice_name: str, setting) -> bool:
    """
    Send Sales Invoice email to customer with PDF attachment.

    Uses ERPNext's email queue and the configured Print Format.

    Args:
        sales_invoice_name: Name of the Sales Invoice
        setting: Shopware Setting document

    Returns:
        bool: True if email was queued successfully
    """
    try:
        si = frappe.get_doc("Sales Invoice", sales_invoice_name)

        # Get customer email - check multiple sources
        customer_email = None

        # 1. Check Contact linked to invoice
        if si.contact_email:
            customer_email = si.contact_email

        # 2. Check Customer primary email
        if not customer_email:
            customer_email = frappe.db.get_value("Customer", si.customer, "email_id")

        # 3. Check billing contact
        if not customer_email:
            billing_contact = frappe.db.get_value(
                "Dynamic Link",
                {"link_doctype": "Customer", "link_name": si.customer, "parenttype": "Contact"},
                "parent"
            )
            if billing_contact:
                customer_email = frappe.db.get_value("Contact", billing_contact, "email_id")

        if not customer_email:
            frappe.log_error(
                f"No email address found for customer {si.customer} (Invoice {si.name})",
                "Shopware Invoice Email"
            )
            return False

        # Get Print Format from setting or use default
        print_format = getattr(setting, 'invoice_print_format', None) or "Legacy Invoice with Payment"

        # Get company name for email
        company_name = frappe.db.get_value("Company", si.company, "company_name") or si.company

        # Build email subject and message
        subject = f"Ihre Rechnung {si.name} - {company_name}"

        # Check if email template is configured
        email_template_name = getattr(setting, 'invoice_email_template', None)

        if email_template_name and frappe.db.exists("Email Template", email_template_name):
            template = frappe.get_doc("Email Template", email_template_name)
            message = frappe.render_template(template.response, {"doc": si})
            if template.subject:
                subject = frappe.render_template(template.subject, {"doc": si})
        else:
            # Default message in German
            message = f"""
<p>Sehr geehrte Damen und Herren,</p>

<p>anbei erhalten Sie Ihre Rechnung <strong>{si.name}</strong> als PDF-Dokument.</p>

<p>Rechnungsbetrag: <strong>{si.get_formatted("grand_total")}</strong></p>

<p>Bei Fragen zu Ihrer Rechnung stehen wir Ihnen gerne zur Verfügung.</p>

<p>Mit freundlichen Grüßen,<br>
{company_name}</p>
            """

        # Generate PDF attachment
        pdf_content = frappe.get_print(
            "Sales Invoice",
            si.name,
            print_format=print_format,
            as_pdf=True
        )

        # Send email with PDF attachment
        frappe.sendmail(
            recipients=[customer_email],
            subject=subject,
            message=message,
            attachments=[{
                "fname": f"Rechnung_{si.name}.pdf",
                "fcontent": pdf_content
            }],
            reference_doctype="Sales Invoice",
            reference_name=si.name,
            now=False  # Queue the email
        )

        frappe.logger("shopware6").info(
            f"Invoice email queued for {si.name} to {customer_email}"
        )
        return True

    except Exception as e:
        frappe.log_error(
            f"Failed to send invoice email for {sales_invoice_name}: {e}",
            "Shopware Invoice Email"
        )
        return False


def handle_transaction_state_change(payload: Dict[str, Any], request_id: str = None, retry_count: int = 0):
    """
    Handle order transaction state change from Shopware webhook.

    This is triggered when a payment status changes in Shopware, for example:
    - PayPal completes payment -> state changes to "paid"
    - Credit card authorization completed
    - SEPA direct debit confirmed

    Args:
        payload: Webhook payload from Shopware
        request_id: Log entry ID for tracking
        retry_count: Number of retry attempts (used for delayed retry when order doesn't exist yet)
    """
    frappe.set_user("Administrator")
    frappe.flags.request_id = request_id

    # Maximum retry attempts and delay configuration
    MAX_RETRIES = 3
    RETRY_DELAYS = [5, 10, 20]  # Seconds to wait before each retry

    try:
        # Extract data from payload
        data = payload.get("data", {})
        order_id = data.get("orderId")
        order_number = data.get("orderNumber")
        transaction_id = data.get("transactionId")
        new_state = data.get("state", "")
        previous_state = data.get("previousState", "")

        if not order_id:
            if request_id:
                update_shopware_log(request_id, status="Error", message="No orderId in payload")
            return

        frappe.logger("shopware6").info(
            f"Transaction state change: Order {order_number}, "
            f"Transaction {transaction_id}, {previous_state} -> {new_state}"
            f" (retry {retry_count}/{MAX_RETRIES})"
        )

        # Find the Sales Order
        sales_order_name = frappe.db.get_value(
            "Sales Order",
            {"shopware_order_id": order_id},
            "name"
        )

        if not sales_order_name:
            # Order doesn't exist yet - this can happen due to race condition
            # where payment webhook arrives before order.placed webhook is processed
            if retry_count < MAX_RETRIES:
                delay = RETRY_DELAYS[retry_count] if retry_count < len(RETRY_DELAYS) else RETRY_DELAYS[-1]
                frappe.logger("shopware6").info(
                    f"Sales Order not found for {order_id}, retrying in {delay}s ({retry_count + 1}/{MAX_RETRIES})"
                )

                # Update log to show pending retry
                if request_id:
                    update_shopware_log(
                        request_id,
                        status="Pending",
                        message=f"Order not found, waiting {delay}s for retry {retry_count + 1}/{MAX_RETRIES}"
                    )

                # Wait and then retry directly (within same request context)
                import time
                time.sleep(delay)

                # Retry by calling this function recursively
                return handle_transaction_state_change(payload, request_id, retry_count + 1)
            else:
                # All retries exhausted
                if request_id:
                    update_shopware_log(
                        request_id,
                        status="Skipped",
                        message=f"No Sales Order found for Shopware order {order_id} after {MAX_RETRIES} retries"
                    )
                frappe.logger("shopware6").warning(
                    f"Payment webhook for order {order_id} skipped: order not found after {MAX_RETRIES} retries"
                )
                return

        # Map the state to ERPNext payment status
        erpnext_status = PAYMENT_STATE_MAP.get(new_state)

        if erpnext_status:
            # Update payment status on Sales Order
            frappe.db.set_value(
                "Sales Order",
                sales_order_name,
                "shopware_payment_status",
                erpnext_status
            )
            frappe.logger("shopware6").info(
                f"Updated {sales_order_name} payment status to: {erpnext_status}"
            )

        # If payment is now "paid" or "completed", create Sales Invoice and Payment Entry
        if new_state in ["paid", "completed", "authorized", "authorize"]:
            setting = frappe.get_doc(SETTING_DOCTYPE)
            messages = []

            # Create Sales Invoice if enabled
            if setting.sync_sales_invoice:
                invoice_result = _create_sales_invoice_for_order(sales_order_name, transaction_id, setting)
                if invoice_result:
                    messages.append(f"Created Invoice {invoice_result}")

                    # Send invoice email if enabled
                    if getattr(setting, 'auto_send_invoice_email', False):
                        if send_invoice_email(invoice_result, setting):
                            messages.append(f"Email sent for {invoice_result}")
                        else:
                            messages.append(f"Email failed for {invoice_result}")

            # Create Payment Entry
            pe_result = create_payment_entry_for_order(sales_order_name, transaction_id)
            if pe_result:
                messages.append(f"Created Payment Entry {pe_result}")

            if messages:
                if request_id:
                    update_shopware_log(
                        request_id,
                        status="Success",
                        message=f"{sales_order_name}: {', '.join(messages)}"
                    )
            else:
                if request_id:
                    update_shopware_log(
                        request_id,
                        status="Skipped",
                        message=f"No documents created for {sales_order_name} (may already exist)"
                    )
        else:
            if request_id:
                update_shopware_log(
                    request_id,
                    status="Success",
                    message=f"Updated payment status for {sales_order_name} to {erpnext_status or new_state}"
                )

        frappe.db.commit()

    except Exception as e:
        frappe.log_error(
            f"Error handling transaction state change: {e}",
            "Shopware Payment Handler"
        )
        if request_id:
            update_shopware_log(request_id, status="Error", exception=str(e))
        raise


def create_payment_entry_for_order(sales_order_name: str, transaction_id: str = None) -> Optional[str]:
    """
    Create a Payment Entry for a Sales Order that has been paid.

    This function handles the case where payment is completed in Shopware
    (e.g., via PayPal, Credit Card) and we need to create the corresponding
    Payment Entry in ERPNext.

    Flow:
    1. Check if Sales Invoice exists for this order
    2. If yes, create Payment Entry against the Invoice
    3. If no invoice, create advance payment against Sales Order

    Args:
        sales_order_name: ERPNext Sales Order name
        transaction_id: Shopware transaction ID (for reference)

    Returns:
        Payment Entry name if created, None otherwise
    """
    from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

    setting = frappe.get_doc(SETTING_DOCTYPE)

    # Get Sales Order details
    so = frappe.get_doc("Sales Order", sales_order_name)

    # Check if there's already a Payment Entry for this order
    existing_pe = frappe.db.exists(
        "Payment Entry Reference",
        {
            "reference_doctype": "Sales Order",
            "reference_name": sales_order_name,
            "docstatus": 1
        }
    )

    if existing_pe:
        frappe.logger("shopware6").info(
            f"Payment Entry already exists for Sales Order {sales_order_name}"
        )
        return None

    # Check for existing Sales Invoice
    sales_invoice_name = frappe.db.get_value(
        "Sales Invoice Item",
        {"sales_order": sales_order_name, "docstatus": 1},
        "parent"
    )

    if sales_invoice_name:
        # Check if invoice already has a payment entry
        existing_pe_for_si = frappe.db.exists(
            "Payment Entry Reference",
            {
                "reference_doctype": "Sales Invoice",
                "reference_name": sales_invoice_name,
                "docstatus": 1
            }
        )

        if existing_pe_for_si:
            frappe.logger("shopware6").info(
                f"Payment Entry already exists for Invoice {sales_invoice_name}"
            )
            return None

        # Create payment against invoice
        return _create_payment_against_invoice(sales_invoice_name, so, setting, transaction_id)
    else:
        # No invoice yet - create advance payment against Sales Order
        return _create_advance_payment(so, setting, transaction_id)


def _create_payment_against_invoice(
    invoice_name: str,
    sales_order: "frappe.Document",
    setting,
    transaction_id: str = None
) -> Optional[str]:
    """Create Payment Entry against a Sales Invoice."""
    from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

    try:
        si = frappe.get_doc("Sales Invoice", invoice_name)

        # Get mode of payment from Sales Order
        mode_of_payment = (
            sales_order.get("shopware_erpnext_mode_of_payment") or
            sales_order.get("mode_of_payment") or
            "PayPal"  # Default for direct payments
        )

        payment_entry = get_payment_entry(
            "Sales Invoice",
            invoice_name,
            bank_account=setting.cash_bank_account
        )

        payment_entry.flags.ignore_mandatory = True
        payment_entry.reference_no = transaction_id or invoice_name
        payment_entry.posting_date = nowdate()
        payment_entry.reference_date = nowdate()
        payment_entry.mode_of_payment = mode_of_payment

        # Add remarks
        payment_entry.remarks = f"Payment for Shopware Order {sales_order.shopware_order_number}"

        payment_entry.insert(ignore_permissions=True)
        payment_entry.submit()

        frappe.logger("shopware6").info(
            f"Created Payment Entry {payment_entry.name} against Invoice {invoice_name}"
        )

        return payment_entry.name

    except Exception as e:
        frappe.log_error(
            f"Failed to create Payment Entry against Invoice {invoice_name}: {e}",
            "Shopware Payment Entry"
        )
        return None


def _create_advance_payment(
    sales_order: "frappe.Document",
    setting,
    transaction_id: str = None
) -> Optional[str]:
    """
    Create an advance Payment Entry against a Sales Order.

    This is used when payment is received before the invoice is created.
    The payment is linked to the Sales Order as an advance.
    """
    from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

    try:
        # Get mode of payment from Sales Order
        mode_of_payment = (
            sales_order.get("shopware_erpnext_mode_of_payment") or
            sales_order.get("mode_of_payment") or
            "PayPal"  # Default for direct payments
        )

        payment_entry = get_payment_entry(
            "Sales Order",
            sales_order.name,
            bank_account=setting.cash_bank_account
        )

        payment_entry.flags.ignore_mandatory = True
        payment_entry.reference_no = transaction_id or sales_order.shopware_order_number or sales_order.name
        payment_entry.posting_date = nowdate()
        payment_entry.reference_date = nowdate()
        payment_entry.mode_of_payment = mode_of_payment

        # Add remarks
        payment_entry.remarks = f"Advance payment for Shopware Order {sales_order.shopware_order_number}"

        payment_entry.insert(ignore_permissions=True)
        payment_entry.submit()

        frappe.logger("shopware6").info(
            f"Created advance Payment Entry {payment_entry.name} for Sales Order {sales_order.name}"
        )

        return payment_entry.name

    except Exception as e:
        frappe.log_error(
            f"Failed to create advance Payment Entry for {sales_order.name}: {e}",
            "Shopware Advance Payment"
        )
        return None


@frappe.whitelist()
def manually_create_payment_entry(sales_order_name: str) -> Dict[str, Any]:
    """
    Manually trigger Payment Entry creation for a Sales Order.

    This can be called from the Sales Order form or via API
    to create a Payment Entry for orders that were paid in Shopware.

    Args:
        sales_order_name: ERPNext Sales Order name

    Returns:
        dict with status and message
    """
    try:
        result = create_payment_entry_for_order(sales_order_name)

        if result:
            return {
                "status": "success",
                "message": f"Created Payment Entry: {result}",
                "payment_entry": result
            }
        else:
            return {
                "status": "skipped",
                "message": "Payment Entry not created (may already exist)"
            }

    except Exception as e:
        return {
            "status": "error",
            "message": str(e)
        }


def _create_sales_invoice_for_order(
    sales_order_name: str,
    transaction_id: str,
    setting
) -> Optional[str]:
    """
    Create Sales Invoice for a paid Sales Order.

    This is called when payment is captured in Shopware to automatically
    create the corresponding Sales Invoice in ERPNext.

    Args:
        sales_order_name: ERPNext Sales Order name
        transaction_id: Shopware transaction ID
        setting: Shopware Setting document

    Returns:
        Sales Invoice name if created, None otherwise
    """
    from erpnext.selling.doctype.sales_order.sales_order import make_sales_invoice

    try:
        # Check if Sales Invoice already exists for this order
        existing_invoice = frappe.db.get_value(
            "Sales Invoice Item",
            {"sales_order": sales_order_name, "docstatus": 1},
            "parent"
        )

        if existing_invoice:
            frappe.logger("shopware6").info(
                f"Sales Invoice {existing_invoice} already exists for {sales_order_name}"
            )
            return None

        # Check if invoice with this transaction ID already exists
        if transaction_id:
            existing = frappe.db.get_value(
                "Sales Invoice",
                {"shopware_transaction_id": transaction_id},
                "name"
            )
            if existing:
                return None

        # Create Sales Invoice from Sales Order
        si = make_sales_invoice(sales_order_name)
        si.naming_series = setting.sales_invoice_series

        if transaction_id:
            si.shopware_transaction_id = transaction_id

        # Copy mode_of_payment from Sales Order
        so_mode_of_payment = frappe.db.get_value(
            "Sales Order", sales_order_name, "shopware_erpnext_mode_of_payment"
        )
        if so_mode_of_payment:
            si.mode_of_payment = so_mode_of_payment

        si.insert(ignore_permissions=True)
        si.submit()

        frappe.logger("shopware6").info(
            f"Created Sales Invoice {si.name} for {sales_order_name}"
        )

        return si.name

    except Exception as e:
        frappe.log_error(
            f"Failed to create Sales Invoice for {sales_order_name}: {e}",
            "Shopware Invoice Creation"
        )
        return None
