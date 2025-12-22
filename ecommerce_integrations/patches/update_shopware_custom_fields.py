"""
Patch to add Shopware 6 custom fields including shopware_active for Item Group.
"""
import frappe

from ecommerce_integrations.shopware6.constants import SETTING_DOCTYPE


def execute():
    frappe.reload_doc("shopware6", "doctype", "shopware_setting")

    # Check if Shopware is enabled
    try:
        settings = frappe.get_doc(SETTING_DOCTYPE)
        if settings.is_enabled():
            from ecommerce_integrations.shopware6.custom_fields import setup_custom_fields
            setup_custom_fields()
    except Exception:
        # If setting doesn't exist or is not enabled, still create the custom fields
        # so they're ready when Shopware is enabled
        from ecommerce_integrations.shopware6.custom_fields import setup_custom_fields
        setup_custom_fields()
