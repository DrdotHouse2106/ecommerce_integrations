"""App-level install hook.

Called once by Frappe's ``install_app()`` flow on a FRESH site install —
the only setup path guaranteed to run unconditionally there. Patches
(``patches.txt``) are NOT executed on a fresh install: Frappe records
every patch as already-applied without running it, since a new site has
no historical data to migrate. Any custom field that used to be created
only via a patch (the pattern several submodules relied on) therefore
never materialised on a fresh install unless the operator happened to
trigger the same setup reactively (e.g. opening and saving a Setting
doctype at least once).

This surfaced as a severe bug: ``ecommerce_source`` on Sales Invoice /
Delivery Note / Payment Entry (shared by Shopware6 and Medusa, defined
once in ``ecommerce_custom_fields.ECOMMERCE_DOWNSTREAM_FIELDS`` but only
ever installed via Medusa's ``setup_custom_fields()``) never existed on
a Shopware6-only fresh install. The ``Ecommerce Sales Invoice``
notification fixture — installed unconditionally for the whole app —
reads ``doc.ecommerce_source`` in its condition, and Frappe's
Notification-condition evaluator raises ``AttributeError`` on a
genuinely undefined Document attribute. Every Sales Invoice save then
crashed notification dispatch.

Idempotent: every ``setup_custom_fields()`` call below goes through
``create_custom_fields(..., update=True)``, safe to call on every
migrate/install regardless of whether the fields already exist.
"""

import frappe


def after_install():
    from ecommerce_integrations.ai_description.custom_fields import (
        setup_custom_fields as setup_ai_description_custom_fields,
    )
    from ecommerce_integrations.medusa.custom_fields import (
        setup_custom_fields as setup_medusa_custom_fields,
    )
    from ecommerce_integrations.shopware6.custom_fields import (
        setup_custom_fields as setup_shopware_custom_fields,
    )

    # Each call is independently best-effort — a failure installing one
    # channel's fields (e.g. an unrelated doctype missing on a minimal
    # install) must not block the others from installing theirs.
    for setup in (
        setup_shopware_custom_fields,
        setup_medusa_custom_fields,
        setup_ai_description_custom_fields,
    ):
        try:
            setup()
        except Exception:
            frappe.log_error(
                title=f"ecommerce_integrations after_install: {setup.__module__} failed",
                message=frappe.get_traceback(),
            )

    frappe.db.commit()
