"""Re-shape the ``Shopware Checkout Field`` rows so XRechnung B2G data
flows into ERPNext's dedicated Customer fields.

Background
----------

The earlier seeded configuration conflated three distinct concepts:

* Shopware's ``custom_po_number`` (front-end labelled "Kommission" /
  "PO Number") — the buyer's internal reference (cost-centre,
  commission). Semantically this is **BT-10 Buyer reference**, which
  ``eu_einvoice`` reads from ``Sales Order.buyer_reference`` (XRechnung
  ``trade.agreement.buyer_reference``). ``Sales Order.po_no`` is a
  different field — **BT-13 Purchase order reference** —
  (``trade.agreement.buyer_order.issuer_assigned_id``), reserved for
  cases where the buyer assigns a distinct PO number.
* Leitweg-ID — German B2G XRechnung routing identifier (BT-49 +
  EAS ``0204``). Needs its own home on the Customer master so the
  operator can see + edit it, and we can render it into XRechnung.
* "Öffentlicher Auftraggeber" flag — toggles the XRechnung profile.

Old state on the operator's site:

* A ``buyer_reference`` row matched ``leitweg_id, leitwegId,
  buyer_reference`` and wrote them all into the generic
  ``Customer.buyer_reference``. Leitweg was mixed with anything else.
* ``is_government_org`` had no mapping at all.
* ``invoice_email`` was ``Info Only`` → silently ignored on import.

This patch
----------

1. Drops the old mis-purposed ``buyer_reference`` row (it was mixing
   Leitweg-ID into ``Customer.buyer_reference``). The ``po_number``
   row stays in charge of routing the buyer reference — its target
   should be ``Sales Order.buyer_reference`` (BT-10), set in the
   ``Shopware Setting.checkout_fields`` table as operator data. (We
   intentionally do not flip the target here: existing installs may
   have re-routed it elsewhere on purpose.)
2. Upserts a ``leitweg_id`` row → Customer Update →
   ``Customer.leitweg_id`` (the dedicated field declared in
   ``shopware6.custom_fields``). A
   ``Customer.before_save`` hook in
   ``shopware6.customer.sync._mirror_leitweg_into_electronic_address``
   automatically copies the value into Alyf's
   ``electronic_address`` (+ EAS scheme ``0204``) so the existing
   XRechnung renderer still picks it up.
3. Upserts an ``is_government_org`` row → Customer Update →
   ``Customer.is_government_org`` so the public-sector flag from
   Shopware lands on the customer master.
4. Promotes ``invoice_email`` from ``Info Only`` to ``Sales Order
   Field`` → ``contact_email`` so the alt-billing-email actually
   overrides the per-order recipient.

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
    # Only run when the dedicated Customer fields actually exist —
    # ``shopware6.custom_fields`` ships them, but on a fresh install
    # this patch may race with the custom-field sync.
    if not frappe.db.has_column("Customer", "leitweg_id"):
        return

    parent = frappe.get_doc(_PARENT_DOCTYPE)
    rows = parent.get(_PARENT_TABLE_FIELD) or []
    changes: list[str] = []

    # 1. Drop the mis-purposed ``buyer_reference`` row (it was a
    #    Leitweg-mixing-pot, not a real BT-10 mapping). The ``po_number``
    #    row should carry BT-10 → ``Sales Order.buyer_reference``; we
    #    don't enforce the target here because operators may have
    #    re-routed it intentionally.
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

    # 2. Leitweg-ID → dedicated Customer.leitweg_id field.
    leitweg_row = _find_row(parent, "leitweg_id")
    if leitweg_row:
        if (leitweg_row.target_field != "leitweg_id"
                or leitweg_row.mapping_type != "Customer Update"):
            leitweg_row.source_field_names = "leitweg_id, leitwegId, leitweg"
            leitweg_row.mapping_type = "Customer Update"
            leitweg_row.target_field = "leitweg_id"
            changes.append("updated leitweg_id row → Customer.leitweg_id")
    else:
        parent.append(_PARENT_TABLE_FIELD, {
            "field_key": "leitweg_id",
            "source_field_names": "leitweg_id, leitwegId, leitweg",
            "mapping_type": "Customer Update",
            "target_field": "leitweg_id",
        })
        changes.append("created leitweg_id row → Customer.leitweg_id")

    # 3. Government-org flag → dedicated Customer.is_government_org field.
    gov_row = _find_row(parent, "is_government_org")
    if gov_row:
        if (gov_row.target_field != "is_government_org"
                or gov_row.mapping_type != "Customer Update"):
            gov_row.source_field_names = "is_government_org, isGovernmentOrg"
            gov_row.mapping_type = "Customer Update"
            gov_row.target_field = "is_government_org"
            changes.append("updated is_government_org row → "
                           "Customer.is_government_org")
    else:
        parent.append(_PARENT_TABLE_FIELD, {
            "field_key": "is_government_org",
            "source_field_names": "is_government_org, isGovernmentOrg",
            "mapping_type": "Customer Update",
            "target_field": "is_government_org",
        })
        changes.append("created is_government_org row → "
                       "Customer.is_government_org")

    # 4. invoice_email: promote from Info Only → Sales Order Field.
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
