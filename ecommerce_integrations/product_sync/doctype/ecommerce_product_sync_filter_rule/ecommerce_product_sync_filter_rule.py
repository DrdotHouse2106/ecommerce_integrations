"""Ecommerce Product Sync Filter Rule — child table.

Validation lives on the parent ``EcommerceProductSync.validate``
because rule semantics (e.g. ``Custom Filter`` requires at least one
rule) are only meaningful in the parent context.
"""

from __future__ import annotations

from frappe.model.document import Document


class EcommerceProductSyncFilterRule(Document):
    pass
