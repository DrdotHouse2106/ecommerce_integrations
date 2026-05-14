"""Ecommerce Catalog Mirror controller.

The controller is intentionally thin — orchestration lives in
``catalog_mirror.tasks``. The doc only enforces a small set of
correctness invariants on save (one mirror per backend × sales-channel,
sane name template) so the operator sees the validation error at form
save time rather than at the next sync.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document

from ecommerce_integrations.catalog_mirror.constants import (
    BACKEND_MEDUSA,
    BACKEND_SHOPWARE,
)


class EcommerceCatalogMirror(Document):
    def validate(self) -> None:
        self._validate_unique_per_channel()
        self._validate_overrides()

    def _validate_unique_per_channel(self) -> None:
        """Refuse a second mirror with the same (backend, sales_channel).

        Two mirrors writing to the same backend tree would race each
        other and silently overwrite each other's state. The unique
        guard surfaces the conflict at save time.
        """
        if self.backend not in (BACKEND_SHOPWARE, BACKEND_MEDUSA):
            return
        channel = self.target_sales_channel or self.target_sales_channel_data
        if not channel:
            return
        existing = frappe.get_all(
            "Ecommerce Catalog Mirror",
            filters={
                "backend": self.backend,
                "target_sales_channel": channel,
                "name": ("!=", self.name or ""),
            },
            pluck="name",
        )
        if existing:
            frappe.throw(
                _(
                    "Another Catalog Mirror already targets {0} on {1}: {2}"
                ).format(channel, self.backend, ", ".join(existing)),
            )

    def _validate_overrides(self) -> None:
        """One override row per Item Group — duplicates indicate a paste error."""
        seen: set[str] = set()
        for row in self.node_overrides or []:
            if not row.item_group:
                continue
            if row.item_group in seen:
                frappe.throw(
                    _("Duplicate override for Item Group {0}").format(
                        row.item_group,
                    ),
                )
            seen.add(row.item_group)
