"""
Setup functions for Shopware 6 Integration
"""

import frappe


def after_install():
    """Called after app installation."""
    from ecommerce_integrations.shopware6.custom_fields import setup_custom_fields

    setup_custom_fields()
    frappe.db.commit()


def before_uninstall():
    """Called before app uninstallation."""
    from ecommerce_integrations.shopware6.custom_fields import remove_custom_fields

    remove_custom_fields()
    frappe.db.commit()
