"""Ecommerce Pull Sync Channel — child table.

Validation lives on the parent EcommercePullSync; this class is a
no-op holder so Frappe finds the controller.
"""

from __future__ import annotations

from frappe.model.document import Document


class EcommercePullSyncChannel(Document):
    pass
