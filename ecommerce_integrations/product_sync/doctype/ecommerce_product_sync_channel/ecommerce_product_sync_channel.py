"""Ecommerce Product Sync Channel — child table.

Validation lives on the parent ``EcommerceProductSync.validate``
because the invariants (unique sales_channel_id, single primary) only
make sense in the parent context.
"""

from __future__ import annotations

from frappe.model.document import Document


class EcommerceProductSyncChannel(Document):
    pass
