"""
Shopware 6 Customer — VAT ID validation + customer-linking helper.
"""

import frappe

from ecommerce_integrations.shopware6.utils import get_logger


def trigger_vat_id_check(customer_name: str, vat_id: str) -> str | None:
    """
    Trigger automatic VAT ID validation using ERPNext Germany's VAT ID Check.

    Creates a VAT ID Check document which will validate the VAT ID against
    the EU VIES database.
    """
    if not vat_id:
        return None

    vat_id = vat_id.strip().upper().replace(" ", "")

    if not frappe.db.exists("DocType", "VAT ID Check"):
        frappe.logger("shopware6").info(
            f"VAT ID Check doctype not found - Germany plugin may not be installed. "
            f"Skipping VAT validation for {customer_name}"
        )
        return None

    existing_check = frappe.db.get_value(
        "VAT ID Check",
        {"party_type": "Customer", "party": customer_name, "party_vat_id": vat_id},
        "name",
    )

    if existing_check:
        frappe.logger("shopware6").info(
            f"VAT ID Check already exists for {customer_name}: {existing_check}"
        )
        return existing_check

    try:
        billing_address = frappe.db.sql(
            """
            SELECT a.name
            FROM `tabAddress` a
            INNER JOIN `tabDynamic Link` dl ON dl.parent = a.name AND dl.parenttype = 'Address'
            WHERE dl.link_doctype = 'Customer' AND dl.link_name = %s AND a.address_type = 'Billing'
            LIMIT 1
            """,
            (customer_name,),
            as_dict=True,
        )
        if billing_address:
            billing_address = billing_address[0].name
        else:
            billing_address = frappe.db.sql(
                """
                SELECT a.name
                FROM `tabAddress` a
                INNER JOIN `tabDynamic Link` dl ON dl.parent = a.name AND dl.parenttype = 'Address'
                WHERE dl.link_doctype = 'Customer' AND dl.link_name = %s
                LIMIT 1
                """,
                (customer_name,),
                as_dict=True,
            )
            if billing_address:
                billing_address = billing_address[0].name

        vat_check = frappe.get_doc({
            "doctype": "VAT ID Check",
            "party_type": "Customer",
            "party": customer_name,
            "party_vat_id": vat_id,
            "party_address": billing_address,
        })

        vat_check.insert(ignore_permissions=True)

        frappe.logger("shopware6").info(
            f"Created VAT ID Check {vat_check.name} for customer {customer_name} with VAT ID {vat_id}"
        )

        try:
            if hasattr(vat_check, "validate_vat_id"):
                vat_check.validate_vat_id()
                vat_check.save(ignore_permissions=True)
            elif hasattr(vat_check, "check_vat_id"):
                vat_check.check_vat_id()
                vat_check.save(ignore_permissions=True)

            # Email notifications for invalid VAT IDs should be configured
            # via ERPNext Notification on "VAT ID Check" doctype with condition:
            # doc.status == "Completed" and doc.is_valid == 0
            vat_check.reload()
        except Exception as validation_error:
            frappe.logger("shopware6").warning(
                f"VAT ID validation failed for {vat_id}: {validation_error}. "
                f"Check can be run manually: {vat_check.name}"
            )

        return vat_check.name

    except Exception as e:
        logger = get_logger("trigger_vat_id_check")
        logger.error(
            f"Failed to create VAT ID Check for {customer_name} with VAT ID {vat_id}",
            exception=e,
            persist=True,
        )
        return None


def _find_existing_customer_to_link(
    email: str,
    company: str,
    customer_name: str,
    customer_id: str,
) -> str | None:
    """
    Find an existing ERPNext customer that can be linked to a Shopware customer.

    Rules:
    1. Only match customers that DON'T already have a shopware_customer_id
       (never overwrite existing links - that breaks data integrity)
    2. For B2B (with company): Match by company name
    3. For B2C: Match by customer_name
    4. Email alone is NOT enough (same email can have multiple companies/persons)
    """
    if not customer_name:
        return None

    filters = {
        "customer_name": customer_name,
        "shopware_customer_id": ["is", "not set"],
    }
    existing = frappe.db.get_value("Customer", filters, "name")

    if existing:
        frappe.logger("shopware6").info(
            f"Found existing customer '{existing}' matching name '{customer_name}' "
            f"(no shopware_customer_id set) - will link to Shopware ID {customer_id}"
        )
        return existing

    # For B2B: Also try matching by company name with email as secondary check
    if company and email:
        filters_with_email = {
            "customer_name": company,
            "email_id": email,
            "shopware_customer_id": ["is", "not set"],
        }
        existing = frappe.db.get_value("Customer", filters_with_email, "name")
        if existing:
            frappe.logger("shopware6").info(
                f"Found existing B2B customer '{existing}' matching company '{company}' + email "
                f"(no shopware_customer_id set) - will link to Shopware ID {customer_id}"
            )
            return existing

    return None


def _sync_customer_number_to_shopware(
    client, erp_customer_name: str, shopware_customer_id: str, current_shopware_number: str | None
) -> None:
    """Push ERPNext's ``Customer.name`` onto Shopware's ``customerNumber``
    when they've drifted apart.

    ERPNext is authoritative for customer numbers going forward — this runs
    unconditionally whenever a Shopware customer gets linked to an ERPNext
    one (regardless of whether that ERPNext customer's "Nach Shopware
    übertragen" checkbox was ever ticked; that flag only gates the separate
    *proactive* ERP-to-Shopware creation push in ``push.py``). Idempotent:
    no-op when the numbers already match, so calling this on every webhook
    is cheap.
    """
    if not shopware_customer_id or not erp_customer_name:
        return
    if erp_customer_name == (current_shopware_number or ""):
        return
    try:
        client.request_patch(f"customer/{shopware_customer_id}", {"customerNumber": erp_customer_name})
        frappe.logger("shopware6").info(
            f"Unified customer number: Shopware customer {shopware_customer_id} "
            f"customerNumber '{current_shopware_number}' -> '{erp_customer_name}' (ERPNext Customer.name)"
        )
    except Exception as e:
        logger = get_logger("sync_customer_number_to_shopware")
        logger.error(
            f"Failed to push customer number '{erp_customer_name}' to Shopware customer "
            f"{shopware_customer_id}",
            exception=e,
            persist=True,
        )
