"""Ecommerce Product Sync controller.

Thin — actual sync logic lives in ``product_sync.tasks``. The doc
enforces correctness invariants at save time so the operator sees
validation errors in the form rather than at next sync run.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document


class EcommerceProductSync(Document):
    def validate(self) -> None:
        self._validate_unique_per_scope()
        self._validate_target_channels()
        self._validate_filter_rules()
        self._validate_overrides()
        self._validate_cron()

    def _validate_unique_per_scope(self) -> None:
        # Refuse another Sync with the same (backend, scope_mode, scope_key)
        # to avoid two Syncs racing on the same set of items.
        scope_key = (
            self.linked_catalog_mirror
            or self.linked_smart_collection
            or self.root_item_group
            or ""
        )
        if not self.backend or not self.scope_mode:
            return
        # TODO(phase-2): once a clean Phase 1 install is verified, narrow
        # this guard further (e.g. compare the resolved item set rather
        # than the raw scope key) — for now the coarse uniqueness check
        # is what mirrors the Catalog Mirror behaviour.
        _ = scope_key  # reserved for the upcoming finer-grained check
        existing = frappe.get_all(
            self.doctype,
            filters={
                "backend": self.backend,
                "scope_mode": self.scope_mode,
                "linked_catalog_mirror": self.linked_catalog_mirror or "",
                "linked_smart_collection": self.linked_smart_collection or "",
                "root_item_group": self.root_item_group or "",
                "name": ("!=", self.name or ""),
            },
            pluck="name",
        )
        if existing:
            frappe.throw(
                _("Another Product Sync already covers the same scope: {0}").format(
                    ", ".join(existing),
                ),
            )

    def _validate_target_channels(self) -> None:
        rows = self.target_sales_channels or []
        if not rows:
            return  # operator may set channels later
        seen: set[str] = set()
        primary_count = 0
        for row in rows:
            sc = (row.sales_channel_id or "").strip()
            if not sc:
                continue
            if sc in seen:
                frappe.throw(_("Duplicate sales channel: {0}").format(sc))
            seen.add(sc)
            if int(row.is_primary or 0):
                primary_count += 1
        if primary_count > 1:
            frappe.throw(_("Mark exactly one target channel as primary, not multiple."))

    def _validate_filter_rules(self) -> None:
        if self.scope_mode != "Custom Filter":
            return
        if not (self.custom_filter_rules or []):
            frappe.throw(
                _("Custom Filter scope requires at least one filter rule."),
            )

    def _validate_overrides(self) -> None:
        seen: set[str] = set()
        for row in self.node_overrides or []:
            ic = row.item_code
            if not ic:
                continue
            if ic in seen:
                frappe.throw(
                    _("Duplicate override for Item {0}").format(ic),
                )
            seen.add(ic)

    def _validate_cron(self) -> None:
        if self.cron_preset != "custom":
            return
        expr = (self.cron_schedule or "").strip()
        if not expr:
            frappe.throw(_("cron_schedule is required when cron_preset is 'custom'."))
        # Defer real cron syntax validation to Phase 6 — keeping the field
        # free-form here avoids depending on croniter for Phase 1 anlage.
