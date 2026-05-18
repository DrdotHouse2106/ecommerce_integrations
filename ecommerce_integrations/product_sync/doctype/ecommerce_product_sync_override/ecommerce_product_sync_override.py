"""Ecommerce Product Sync Override — child table.

Validation lives on the parent ``EcommerceProductSync.validate``
because the duplicate-item-code check only makes sense across all
rows of a single parent.
"""

from __future__ import annotations

from frappe.model.document import Document


class EcommerceProductSyncOverride(Document):
    pass
