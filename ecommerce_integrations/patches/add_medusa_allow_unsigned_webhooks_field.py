"""Reload Medusa Setting so the new ``allow_unsigned_webhooks`` field exists.

Idempotent: ``reload_doc`` is safe to call on every migrate; the field
default (``0``) means existing installs default to the safe behaviour
(reject unsigned webhooks).

No-op on sites where the Medusa Setting doctype isn't installed.
"""

import frappe


def execute():
    if not frappe.db.exists("DocType", "Medusa Setting"):
        return
    frappe.reload_doc("medusa", "doctype", "medusa_setting")
