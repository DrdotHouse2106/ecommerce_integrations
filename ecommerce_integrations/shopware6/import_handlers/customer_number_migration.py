"""One-time migration: seed the unified customer-number counter and align
every already-linked ERPNext customer's number onto Shopware.

Prerequisite for the "Nach Shopware übertragen" feature
(``shopware6/customer/push.py``, ``numbering.py``): the counter that
allocates new numbers must start above the current max of BOTH systems
combined, otherwise a newly-allocated ERPNext number could collide with an
old, still-live Shopware customer that was never linked to any ERPNext
record. ``numbering.allocate_next_customer_number`` refuses to run at all
until this migration has seeded the counter — that's the runtime safety
rail, not just documentation.

Manually-triggered button, not a ``patches.txt`` entry: writing to a live
third-party system during ``bench migrate`` is unattended and unmonitored
(same reasoning as ``category_importer.py``, whose skeleton this mirrors).
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe import _

from ecommerce_integrations.shopware6.connection import temp_shopware_session
from ecommerce_integrations.shopware6.constants import CUSTOMER_ID_FIELD, SETTING_DOCTYPE
from ecommerce_integrations.shopware6.customer.numbering import (
    is_counter_initialized,
    seed_customer_number_counter,
)
from ecommerce_integrations.shopware6.customer.vat import _sync_customer_number_to_shopware
from ecommerce_integrations.shopware6.utils import create_shopware_log, update_shopware_log

_PAGE_SIZE = 100


@frappe.whitelist()
def initialize_customer_number_counter_and_push() -> dict[str, Any]:
    """Entry point for the "Kundennummern-Zähler initialisieren & bestehende
    Kunden abgleichen" button. Runs as a background job — paginating every
    Shopware customer to find the safe starting counter value can exceed
    the web worker's request timeout on a large customer base."""
    frappe.only_for("System Manager")

    setting = frappe.get_doc(SETTING_DOCTYPE)
    if not setting.is_enabled():
        frappe.throw(_("Bitte zuerst die Shopware-Integration aktivieren"))
    if is_counter_initialized():
        frappe.throw(_("Der Kundennummern-Zähler wurde bereits initialisiert."))

    log = create_shopware_log(
        status="Queued",
        method="customer_number_migration",
        message=_("Kundennummern-Migration wurde eingereiht..."),
        make_new=True,
    )

    frappe.enqueue(
        _run_customer_number_migration,
        queue="long",
        timeout=3600,
        job_name=f"shopware6_customer_number_migration_{log.name}",
        request_id=log.name,
        enqueue_after_commit=True,
    )

    return {"queued": True, "log": log.name}


@temp_shopware_session
def _run_customer_number_migration(client, request_id: str) -> None:
    stats: dict[str, Any] = {"pushed": 0, "errors": []}

    previous_skip_flag = getattr(frappe.flags, "skip_shopware_sync", False)
    frappe.flags.skip_shopware_sync = True
    try:
        erp_max = _max_numeric_erp_customer_name()
        shopware_max = _max_numeric_shopware_customer_number(client)
        starting_value = max(erp_max, shopware_max)
        seed_customer_number_counter(starting_value)

        linked = frappe.get_all(
            "Customer",
            filters={CUSTOMER_ID_FIELD: ["is", "set"]},
            fields=["name", CUSTOMER_ID_FIELD],
        )
        for row in linked:
            try:
                response = client.request_get(f"customer/{row.get(CUSTOMER_ID_FIELD)}")
                current = response.data if response else None
                _sync_customer_number_to_shopware(
                    client,
                    row.name,
                    row.get(CUSTOMER_ID_FIELD),
                    (current or {}).get("customerNumber"),
                )
                stats["pushed"] += 1
            except Exception as e:  # noqa: BLE001
                stats["errors"].append(f"{row.name}: {e}")
            frappe.db.commit()

        update_shopware_log(
            request_id,
            status="Success" if not stats["errors"] else "Error",
            message=_(
                "Kundennummern-Zähler gestartet bei {0}. {1} bereits verknüpfte Kunden "
                "abgeglichen, {2} Fehler."
            ).format(starting_value, stats["pushed"], len(stats["errors"])),
            exception="\n".join(stats["errors"]) if stats["errors"] else None,
        )
        frappe.db.set_value(
            SETTING_DOCTYPE, SETTING_DOCTYPE, "customer_number_counter_seed", str(starting_value)
        )
        frappe.db.commit()
    except Exception as e:
        update_shopware_log(request_id, status="Error", exception=str(e))
        raise
    finally:
        frappe.flags.skip_shopware_sync = previous_skip_flag


def _max_numeric_erp_customer_name() -> int:
    row = frappe.db.sql(
        "SELECT MAX(CAST(`name` AS UNSIGNED)) FROM `tabCustomer` WHERE `name` REGEXP '^[0-9]+$'"
    )
    return int(row[0][0]) if row and row[0][0] else 0


def _max_numeric_shopware_customer_number(client) -> int:
    """Scan ALL Shopware customers (not just already-linked ones) — an old,
    unlinked, still-live Shopware account must not collide with a
    newly-allocated ERPNext number later."""
    highest = 0
    page = 1
    while True:
        response = client.request_post(
            "search/customer", {"page": page, "limit": _PAGE_SIZE, "includes": {"customer": ["customerNumber"]}}
        )
        customers = response.data or []
        if not customers:
            break
        for c in customers:
            number = c.get("customerNumber") or ""
            if number.isdigit():
                highest = max(highest, int(number))
        if len(customers) < _PAGE_SIZE:
            break
        page += 1
    return highest
