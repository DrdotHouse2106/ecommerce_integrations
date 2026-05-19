"""Pre-fill ``is_deferred_payment`` on existing Shopware Payment Method
Mapping rows so the new acknowledgment-gating logic in
``payment_handler._mark_order_ready`` starts with the operator's
already-configured methods classified correctly.

Heuristic mirrors ``_DEFERRED_PAYMENT_TOKENS`` in
``shopware6.order.payment_handler``. Idempotent: only flips rows where
the field is unset (NULL/0) and the technicalName matches a
deferred-payment token. Operator-set values are preserved.

No-op when the Shopware Setting doctype isn't installed on this site.
"""

import frappe

_DEFERRED_TOKENS = (
    "prepayment", "vorkasse",
    "invoice", "rechnung", "kaufaufrechnung",
    "pui",
    "cashondelivery", "cashpayment", "nachnahme", "cod",
    "sepa", "lastschrift", "directdebit", "debit",
)


def _flatten(name: str) -> str:
    """Lowercase + strip separators so ``s_e_p_a`` matches token ``sepa``."""
    flat = (name or "").lower()
    for ch in ("_", "-", " ", ".", "/"):
        flat = flat.replace(ch, "")
    return flat


def execute() -> None:
    if not frappe.db.exists("DocType", "Shopware Payment Method Mapping"):
        return

    # Make sure the new column exists before we try to update it. On a
    # fresh checkout the migrate that creates the column may not have
    # run yet — this patch is listed after that migrate in patches.txt
    # so this is a safety net for partial installs.
    columns = frappe.db.sql("DESCRIBE `tabShopware Payment Method Mapping`")
    if not any(c[0] == "is_deferred_payment" for c in columns):
        return

    rows = frappe.db.sql(
        """SELECT name, shopware_method, is_deferred_payment
           FROM `tabShopware Payment Method Mapping`""",
        as_dict=True,
    )

    flipped = 0
    for row in rows:
        # Don't overwrite operator-set values
        if row.is_deferred_payment:
            continue
        method = _flatten(row.shopware_method)
        if not method:
            continue
        if any(token in method for token in _DEFERRED_TOKENS):
            frappe.db.set_value(
                "Shopware Payment Method Mapping", row.name,
                "is_deferred_payment", 1,
                update_modified=False,
            )
            flipped += 1

    if flipped:
        frappe.db.commit()
        frappe.logger("shopware6").info(
            f"seed_payment_method_deferred_flag: classified {flipped} rows "
            f"as deferred (Vorkasse/Rechnung/SEPA/COD pattern)"
        )
