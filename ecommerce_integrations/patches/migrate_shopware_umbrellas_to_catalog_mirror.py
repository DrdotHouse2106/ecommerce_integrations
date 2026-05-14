"""Migrate single-rule Shopware umbrella Smart Collections to Catalog Mirror.

The Catalog Mirror module supersedes the "one Smart Collection per Item
Group umbrella, replicated per sales channel" pattern that operators
used before Phase 1 shipped. This patch detects those umbrellas — a
Smart Collection with exactly one ``Item Group`` rule (operator
``equals`` or ``descends_from``) and at least one enabled Shopware
target — and creates the equivalent Catalog Mirror docs.

Migration is *non-destructive*:

- The original Smart Collection is left in place.
- Its Shopware targets are disabled (``enabled=0``) so the SC stops
  pushing items to that channel, but the row stays so the operator can
  re-enable it as a one-click rollback if the Mirror misbehaves.
- A breadcrumb is written to each disabled target's ``last_error``
  field naming the Mirror that replaced it and the date.

Idempotent: re-running the patch finds the existing Mirror via
``(backend, target_sales_channel, root_item_group)`` and skips. SCs
with multi-rule combinators, unsupported operators or no matching rule
are logged and skipped — the operator must migrate those manually.

The patch is no-op safe when the Catalog Mirror doctype isn't
installed on this site (defensive guard for slimmer subset installs).
"""

from __future__ import annotations

import frappe
from frappe.utils import nowdate


_MIRROR_DOCTYPE = "Ecommerce Catalog Mirror"
_SC_DOCTYPE = "Ecommerce Smart Collection"
_SC_TARGET_DOCTYPE = "Ecommerce Smart Collection Target"
_SC_RULE_DOCTYPE = "Ecommerce Smart Collection Rule"

# Operators we know how to translate to a Mirror root. ``descends_from``
# matches the Mirror's nested-set walk exactly; ``equals`` is the
# single-IG case where the walk degenerates to one node + descendants
# (Mirror always includes descendants, so equals → still maps cleanly).
_SUPPORTED_OPERATORS = ("equals", "descends_from")


def execute() -> None:
    # Defensive guard: skip cleanly on sites that don't carry the
    # Catalog Mirror doctype (slimmer subset installs).
    if not frappe.db.exists("DocType", _MIRROR_DOCTYPE):
        return
    if not frappe.db.exists("DocType", _SC_DOCTYPE):
        return

    summary = {
        "created": 0,
        "skipped_existing": 0,
        "skipped_invalid_rules": 0,
        "skipped_no_target": 0,
        "targets_disabled": 0,
    }

    sc_names = frappe.get_all(_SC_DOCTYPE, pluck="name")
    for sc_name in sc_names:
        sc = frappe.get_doc(_SC_DOCTYPE, sc_name)

        shopware_targets = [
            t for t in (sc.targets or [])
            if (t.backend or "") == "Shopware" and int(t.enabled or 0) == 1
        ]
        if not shopware_targets:
            summary["skipped_no_target"] += 1
            continue

        # Rule shape we can migrate automatically: exactly one rule,
        # Item Group type, supported operator, non-empty value.
        rules = list(sc.rules or [])
        if len(rules) != 1:
            _warn_unmigratable(
                sc_name,
                f"expected exactly one rule, found {len(rules)}",
            )
            summary["skipped_invalid_rules"] += 1
            continue

        rule = rules[0]
        rule_type = (rule.rule_type or "").strip()
        operator = (rule.operator or "").strip()
        value = (rule.value or "").strip()
        if rule_type != "Item Group":
            _warn_unmigratable(
                sc_name,
                f"rule_type={rule_type!r} (need 'Item Group')",
            )
            summary["skipped_invalid_rules"] += 1
            continue
        if operator not in _SUPPORTED_OPERATORS:
            _warn_unmigratable(
                sc_name,
                f"operator={operator!r} "
                f"(need one of {_SUPPORTED_OPERATORS})",
            )
            summary["skipped_invalid_rules"] += 1
            continue
        if not value:
            _warn_unmigratable(sc_name, "empty Item Group value")
            summary["skipped_invalid_rules"] += 1
            continue
        if not frappe.db.exists("Item Group", value):
            _warn_unmigratable(
                sc_name, f"Item Group {value!r} does not exist",
            )
            summary["skipped_invalid_rules"] += 1
            continue

        for target in shopware_targets:
            sales_channel = (target.sales_channel or "").strip()
            if not sales_channel:
                continue

            existing = frappe.db.exists(
                _MIRROR_DOCTYPE,
                {
                    "backend": "Shopware",
                    "target_sales_channel": sales_channel,
                    "root_item_group": value,
                },
            )
            if existing:
                summary["skipped_existing"] += 1
                # Re-disable the SC target if it slipped back to
                # enabled after a previous migration — keeps the
                # idempotent re-run truly idempotent.
                if int(target.enabled or 0) == 1:
                    _disable_target(sc_name, target, existing)
                    summary["targets_disabled"] += 1
                continue

            mirror_title = _build_mirror_title(sc.title or sc_name)
            new_name = _insert_mirror(
                title=mirror_title,
                target_sales_channel=sales_channel,
                root_item_group=value,
                external_root_id=(target.external_id or "") or None,
            )
            summary["created"] += 1

            _disable_target(sc_name, target, new_name)
            summary["targets_disabled"] += 1

    frappe.db.commit()  # noqa: SLF001 — persist before next patch in chain

    print(  # noqa: T201 — patch output is expected on stdout
        "[migrate_shopware_umbrellas_to_catalog_mirror] "
        f"created={summary['created']} "
        f"skipped_existing={summary['skipped_existing']} "
        f"skipped_invalid_rules={summary['skipped_invalid_rules']} "
        f"skipped_no_target={summary['skipped_no_target']} "
        f"targets_disabled={summary['targets_disabled']}"
    )


def _build_mirror_title(sc_title: str) -> str:
    """Append the Catalog Mirror suffix while keeping the brand hint.

    Operators keep multiple SCs with the same Item Group umbrella
    distinguished by a parenthesised brand suffix (e.g. ``Produkte
    (Test GmbH)``). We preserve the suffix so the Mirror list view
    still surfaces the per-channel hint.
    """
    base = (sc_title or "").strip()
    suffix = " — Catalog Mirror"
    if base.endswith(suffix):
        return base
    return f"{base}{suffix}"


def _insert_mirror(
    *,
    title: str,
    target_sales_channel: str,
    root_item_group: str,
    external_root_id: str | None,
) -> str:
    """Create the Mirror doc and return its name.

    ``flags.ignore_links`` because ``target_sales_channel`` is a Link to
    a child-table row (``Shopware Sales Channel`` is parented under
    ``Shopware Setting``) — Frappe's link-existence check can't reach
    into child tables, so we skip it; resolver / adapter look the
    channel up via the parent doctype at sync time.
    """
    doc = frappe.get_doc({
        "doctype": _MIRROR_DOCTYPE,
        "title": title,
        "backend": "Shopware",
        "is_active": 1,
        "target_sales_channel": target_sales_channel,
        "root_item_group": root_item_group,
        "external_root_id": external_root_id or "",
        "orphan_policy": "keep",
        "sync_status": "pending",
        "name_template": "{{ item_group.item_group_name }}",
    })
    doc.flags.ignore_links = True
    doc.flags.ignore_permissions = True
    doc.insert()
    return doc.name


def _disable_target(sc_name: str, target, mirror_name: str) -> None:
    """Disable the SC target and stamp a breadcrumb on ``last_error``.

    The breadcrumb keeps the rollback trivially discoverable: the
    operator opens the target row, reads which Mirror replaced it, and
    can either re-enable the target (rollback) or delete it after
    verifying the Mirror works.
    """
    note = (
        f"Migrated to Catalog Mirror {mirror_name} on {nowdate()} — "
        f"disabled by migration. Delete this Smart Collection's "
        f"Shopware target after verifying the Mirror works."
    )
    frappe.db.set_value(
        _SC_TARGET_DOCTYPE,
        target.name,
        {"enabled": 0, "last_error": note},
        update_modified=False,
    )


def _warn_unmigratable(sc_name: str, reason: str) -> None:
    """Emit a warning on stdout — operator must migrate this SC manually."""
    print(  # noqa: T201 — patch output is expected on stdout
        "[migrate_shopware_umbrellas_to_catalog_mirror] "
        f"skip {sc_name}: {reason}"
    )
