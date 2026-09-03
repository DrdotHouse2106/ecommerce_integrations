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

Same bug, second occurrence (2026-09): ``Item Group.shopware_category_id``
(and the other Catalog Mirror fields) only ever got installed via the
``setup_catalog_mirror`` patch — on a fresh install that patch is marked
applied without running, so the category importer's raw SQL WHERE clause
on ``shopware_category_id`` crashed with
``OperationalError: Unknown column``. A dedicated follow-up patch
(``ensure_catalog_mirror_item_group_fields``) was written specifically to
retroactively repair sites that hit this — but being a patches.txt entry
itself, it is *just as vulnerable* on a fresh install as the patch it was
fixing. That is the actual lesson: patching around a fresh-install gap
with another patch doesn't close the gap, it just moves it. The real fix
has to live here.

Audited every patch in ``patches/`` that installs its own custom fields
(2026-09) and wired in every one whose target doctype is guaranteed to
exist by the time ``after_install`` runs (this app's own doctypes, or
core Frappe/ERPNext doctypes — both are synced before ``after_install``
fires). ``install_downstream_ecommerce_fields`` and
``ensure_catalog_mirror_item_group_fields`` are deliberately omitted
below: the former just re-calls ``setup_medusa_custom_fields`` (already
in this list), the latter defines the exact same field dict as
``setup_catalog_mirror`` (also already in this list) — calling either
again would be a harmless but pointless duplicate.
"""

import frappe
from frappe import _


def _all_custom_field_setups():
    """Every "install my own custom fields" entry point across the app —
    shared by :func:`after_install` (fresh installs) and
    :func:`repair_custom_fields` (manual repair on a site that already
    exists but is missing some of these, e.g. because it was itself
    created as a fresh install before this list was comprehensive)."""
    from ecommerce_integrations.ai_description.custom_fields import (
        setup_custom_fields as setup_ai_description_custom_fields,
    )
    from ecommerce_integrations.medusa.custom_fields import (
        setup_custom_fields as setup_medusa_custom_fields,
    )
    from ecommerce_integrations.patches.add_brand_sync_hash import execute as setup_brand_sync_hash
    from ecommerce_integrations.patches.add_canonical_store_to_ecommerce_item import (
        execute as setup_canonical_store_field,
    )
    from ecommerce_integrations.patches.add_image_map_to_ecommerce_item import (
        execute as setup_image_map_field,
    )
    from ecommerce_integrations.patches.add_item_additional_groups_field import (
        execute as setup_item_additional_groups_field,
    )
    from ecommerce_integrations.patches.add_item_ecommerce_channel_overrides import (
        execute as setup_item_channel_overrides,
    )
    from ecommerce_integrations.patches.add_price_list_tax_flag import (
        execute as setup_price_list_tax_flag,
    )
    from ecommerce_integrations.patches.add_shopware_item_custom_field_mappings import (
        execute as setup_shopware_item_custom_field_mappings,
    )
    from ecommerce_integrations.patches.add_sync_visibilities_and_default_channel import (
        execute as setup_sync_visibilities_and_default_channel,
    )
    from ecommerce_integrations.patches.setup_catalog_mirror import (
        execute as setup_catalog_mirror_custom_fields,
    )
    from ecommerce_integrations.patches.setup_product_sync import (
        execute as setup_product_sync_custom_fields,
    )
    from ecommerce_integrations.shopware6.custom_fields import (
        setup_custom_fields as setup_shopware_custom_fields,
    )

    return (
        setup_shopware_custom_fields,
        setup_medusa_custom_fields,
        setup_ai_description_custom_fields,
        setup_catalog_mirror_custom_fields,
        setup_product_sync_custom_fields,
        setup_brand_sync_hash,
        setup_canonical_store_field,
        setup_image_map_field,
        setup_item_additional_groups_field,
        setup_item_channel_overrides,
        setup_price_list_tax_flag,
        setup_shopware_item_custom_field_mappings,
        setup_sync_visibilities_and_default_channel,
    )


def after_install():
    # Each call is independently best-effort — a failure installing one
    # channel's fields (e.g. an unrelated doctype missing on a minimal
    # install) must not block the others from installing theirs.
    for setup in _all_custom_field_setups():
        try:
            setup()
        except Exception:
            frappe.log_error(
                title=f"ecommerce_integrations after_install: {setup.__module__} failed",
                message=frappe.get_traceback(),
            )

    frappe.db.commit()


@frappe.whitelist()
def repair_custom_fields() -> dict:
    """Manual re-run of every custom-field setup, for a site that already
    exists (so ``after_install`` won't fire again) but is missing some of
    these fields — e.g. it was created as a fresh install before a given
    setup function was added to :func:`_all_custom_field_setups`, or a
    ``bench migrate`` was interrupted partway. Same idempotent calls as
    ``after_install``, just triggerable from the UI instead of needing
    ``bench execute`` — useful when the operator has no shell/terminal
    access to the bench (e.g. a managed hosting plan).
    """
    frappe.only_for("System Manager")

    failures = []
    for setup in _all_custom_field_setups():
        try:
            setup()
        except Exception:
            failures.append(setup.__module__)
            frappe.log_error(
                title=f"ecommerce_integrations repair_custom_fields: {setup.__module__} failed",
                message=frappe.get_traceback(),
            )

    frappe.db.commit()

    if failures:
        return {
            "status": "partial",
            "message": _(
                "Custom Fields aktualisiert, aber {0} Schritt(e) sind fehlgeschlagen: {1}. "
                "Details im Error Log."
            ).format(len(failures), ", ".join(failures)),
        }
    return {"status": "success", "message": _("Alle Custom Fields wurden geprüft/aktualisiert.")}
