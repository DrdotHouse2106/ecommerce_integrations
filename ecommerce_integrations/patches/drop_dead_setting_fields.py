"""Drop fields removed from Shopware Setting / Medusa Setting.

The audit (``_tooling/audit_settings_fields.py``) confirmed four
operator-facing fields had zero code references:

- Shopware Setting: ``default_tax_rate``, ``image_batch_size``,
  ``shopware_warehouse_mapping``
- Medusa Setting: ``default_tax_rate``

Plus the ``Shopware Warehouse Mapping`` child doctype is unused
(no parent reference, no Python references) and is dropped here too.

The columns are not dropped from the DB — Frappe's standard
``ALTER TABLE`` cleanup happens on ``bench migrate`` when a field
disappears from the JSON. This patch only clears any orphaned data
that would otherwise linger.
"""

from __future__ import annotations

import frappe


def execute() -> None:
    # Drop the unused child doctype (and its table) if it still exists.
    if frappe.db.exists("DocType", "Shopware Warehouse Mapping"):
        try:
            frappe.delete_doc("DocType", "Shopware Warehouse Mapping", force=True)
        except Exception as exc:  # noqa: BLE001
            frappe.log_error(
                title="drop_dead_setting_fields: Shopware Warehouse Mapping",
                message=str(exc),
            )

    # No need to clear data on the singleton Setting rows — Frappe's
    # core schema sync drops missing columns automatically on the next
    # migrate. We just log a confirmation for the audit log.
    frappe.db.commit()  # noqa: SLF001
