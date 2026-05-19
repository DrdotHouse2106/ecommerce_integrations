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
        # Refuse another Sync with the same backend + scope. Smart Collection
        # scope is multi-row, so compare the normalized collection set instead
        # of the legacy single-link field.
        if not self.backend or not self.scope_mode:
            return

        if self.scope_mode == "Smart Collection":
            current_set = _smart_collection_set(self)
            if not current_set:
                frappe.throw(
                    _("Smart Collection scope requires at least one linked collection."),
                )
            candidates = frappe.get_all(
                self.doctype,
                filters={
                    "backend": self.backend,
                    "scope_mode": self.scope_mode,
                    "name": ("!=", self.name or ""),
                },
                pluck="name",
            )
            duplicates = []
            for name in candidates:
                other = frappe.get_doc(self.doctype, name)
                if _smart_collection_set(other) == current_set:
                    duplicates.append(name)
            if duplicates:
                frappe.throw(
                    _("Another Product Sync already covers the same Smart Collection scope: {0}").format(
                        ", ".join(duplicates),
                    ),
                )
            return

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


def _smart_collection_set(doc) -> tuple[str, ...]:
    names = {
        (row.smart_collection or "").strip()
        for row in (doc.linked_smart_collections or [])
        if (row.smart_collection or "").strip()
    }
    legacy = (doc.linked_smart_collection or "").strip()
    if legacy:
        names.add(legacy)
    return tuple(sorted(names))
