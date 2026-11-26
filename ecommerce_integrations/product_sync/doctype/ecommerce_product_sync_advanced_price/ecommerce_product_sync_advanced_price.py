"""Ecommerce Product Sync Advanced Price — child table.

Maps an ERPNext Price List to a backend pricing rule (Shopware rule UUID).
The Sync pushes that list's price, plus its quantity tiers from Pricing
Rules, as advanced prices under the rule.
"""

from __future__ import annotations

from frappe.model.document import Document


class EcommerceProductSyncAdvancedPrice(Document):
    pass
