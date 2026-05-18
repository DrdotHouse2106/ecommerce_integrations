"""Ecommerce Pull Sync controller.

Configures the inbound (Backend → ERPNext) sync direction. The actual
fetch orchestration lives in :mod:`product_sync.pull_tasks`; this
controller only enforces correctness invariants at save time.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document


class EcommercePullSync(Document):
    def validate(self) -> None:
        self._validate_unique_per_backend()
        self._validate_cron()
        self._validate_at_least_one_target()

    def _validate_unique_per_backend(self) -> None:
        existing = frappe.get_all(
            self.doctype,
            filters={
                "backend": self.backend,
                "name": ("!=", self.name or ""),
            },
            pluck="name",
        )
        if existing:
            frappe.throw(
                _("Another Pull Sync already exists for backend {0}: {1}. "
                  "Consolidate into one doc — multiple pulls per backend "
                  "race each other on the same watermark.").format(
                    self.backend, ", ".join(existing),
                ),
            )

    def _validate_cron(self) -> None:
        if self.cron_preset != "custom":
            return
        if not (self.cron_schedule or "").strip():
            frappe.throw(
                _("cron_schedule is required when cron_preset is 'custom'."),
            )

    def _validate_at_least_one_target(self) -> None:
        if not any([
            int(self.pull_orders or 0),
            int(self.pull_customers or 0),
            int(self.pull_inventory or 0),
        ]):
            frappe.throw(
                _("Mindestens eine Pull-Quelle muss aktiv sein "
                  "(Bestellungen, Kunden oder Bestand)."),
            )
