"""Re-shape the ``Shopware Checkout Field`` rows so XRechnung B2G data
flows into the right ERPNext fields, hooking into Alyf's
``eu_einvoice`` plugin instead of inventing parallel fields.

Background
----------

The earlier seeded configuration conflated two distinct EN-16931 fields:

* ``BT-10`` Leitweg-ID — only relevant for invoicing German public-sector
  buyers, lives on ``Customer.electronic_address`` (Alyf field) with
  ``Customer.electronic_address_scheme = '0204'`` identifying it as a
  Leitweg-ID per the BMI EAS-code list.
* ``BT-13`` Buyer reference — generic per-order field (customer's PO,
  cost-centre); already correctly mapped via ``po_number`` →
  ``Sales Order.po_no``.

Old state on the operator's site:

* ``buyer_reference`` row matched ``leitweg_id, leitwegId,
  buyer_reference`` and wrote them all into the generic
  ``Customer.buyer_reference``. Leitweg never reached Alyf's
  ``electronic_address``.
* ``is_government_org`` had no mapping at all.
* ``invoice_email`` was ``Info Only`` → silently ignored on import.

This patch
----------

1. Drops the old ``buyer_reference`` row (its job is covered by
   ``po_number`` already).
2. Creates a new ``leitweg_id`` row (Customer Update →
   ``electronic_address``). The companion EAS-scheme ``0204`` is
   stamped by ``shopware6.customer.sync._ensure_leitweg_eas_scheme``
   whenever the Leitweg-ID is non-empty.
3. Promotes ``invoice_email`` to ``Sales Order Field`` →
   ``contact_email`` so the alt-billing-email actually overrides the
   per-order recipient.

``is_government_org`` is deliberately left out of the auto-mapping —
whether a customer counts as "öffentlicher Auftraggeber" is an Alyf
``einvoice_profile`` decision (``XRECHNUNG_3.0`` vs others) that the
operator should curate manually on the customer master, not flip per
Shopware order. Storing the Shopware flag elsewhere risks two sources
of truth diverging silently.

All steps are idempotent and no-op safe (skip when the doctype is
missing on this site).
"""

import frappe

# Canonical shared child doctype (the legacy ``Shopware Checkout Field``
# table is orphaned: ``Shopware Setting.checkout_fields`` was rewired to
# ``Ecommerce Checkout Field`` so Shopware and Medusa share the same
# mapping schema). The doctype's field for the comma-separated source
# names is ``source_field_names`` here, not ``source_field_names``.
_DOCTYPE = "Ecommerce Checkout Field"
_PARENT_DOCTYPE = "Shopware Setting"
_PARENT_TABLE_FIELD = "checkout_fields"


def _find_row(parent_doc, field_key: str):
    for row in (parent_doc.get(_PARENT_TABLE_FIELD) or []):
        if (row.field_key or "") == field_key:
            return row
    return None


def execute() -> None:
    if not frappe.db.exists("DocType", _DOCTYPE):
        return
    if not frappe.db.exists("DocType", _PARENT_DOCTYPE):
        return
    # Only attempt to wire Alyf's ``electronic_address`` when the field
    # actually exists — otherwise we'd write into thin air on sites that
    # don't have ``eu_einvoice`` installed.
    if not frappe.db.has_column("Customer", "electronic_address"):
        return

    parent = frappe.get_doc(_PARENT_DOCTYPE)
    rows = parent.get(_PARENT_TABLE_FIELD) or []
    changes: list[str] = []

    # 1. Drop the misnamed ``buyer_reference`` row (covered by
    #    ``po_number`` → ``Sales Order.po_no``).
    keep = [r for r in rows if (r.field_key or "") != "buyer_reference"]
    if len(keep) != len(rows):
        # Re-assign idx so the child table stays compact
        parent.set(_PARENT_TABLE_FIELD, [])
        for i, r in enumerate(keep, start=1):
            d = r.as_dict()
            d.pop("name", None)
            d["idx"] = i
            parent.append(_PARENT_TABLE_FIELD, d)
        changes.append("deleted buyer_reference row")

    # 2. Real Leitweg-ID mapping → eu_einvoice's electronic_address.
    leitweg_row = _find_row(parent, "leitweg_id")
    if leitweg_row:
        if (leitweg_row.target_field != "electronic_address"
                or leitweg_row.mapping_type != "Customer Update"):
            leitweg_row.source_field_names = "leitweg_id, leitwegId, leitweg"
            leitweg_row.mapping_type = "Customer Update"
            leitweg_row.target_field = "electronic_address"
            changes.append("updated leitweg_id row → electronic_address")
    else:
        parent.append(_PARENT_TABLE_FIELD, {
            "field_key": "leitweg_id",
            "source_field_names": "leitweg_id, leitwegId, leitweg",
            "mapping_type": "Customer Update",
            "target_field": "electronic_address",
        })
        changes.append("created leitweg_id row → electronic_address")

    # 3. invoice_email: promote from Info Only → Sales Order Field.
    inv_row = _find_row(parent, "invoice_email")
    if inv_row and inv_row.mapping_type != "Sales Order Field":
        inv_row.mapping_type = "Sales Order Field"
        inv_row.target_field = "contact_email"
        changes.append("invoice_email: Info Only → Sales Order Field "
                       "(contact_email)")

    if not changes:
        return

    parent.flags.ignore_permissions = True
    parent.flags.ignore_validate_update_after_submit = True
    parent.save(ignore_permissions=True)
    frappe.db.commit()
    frappe.logger("shopware6").info(
        "fix_shopware_checkout_field_b2g_mapping: " + "; ".join(changes)
    )
