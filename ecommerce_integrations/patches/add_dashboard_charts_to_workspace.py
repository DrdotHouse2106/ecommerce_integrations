"""Wire the three Ecommerce Dashboard Charts into the workspace body.

The ``Ecommerce Integrations`` workspace was created with
``is_standard=None``, so ``bench migrate`` no longer re-syncs its
content from the shipped JSON. This patch reapplies the chart blocks
(``Sync Runs (Daily)``, ``Items Synced (Daily)``, ``AI Descriptions
Generated (Daily)``) — the corresponding ``Dashboard Chart`` rows are
synced separately by Frappe's standard ``sync_dashboard`` step on
migrate.

Idempotent: skips when the workspace is missing, when the charts are
missing, or when the content already references all three charts.
"""

from __future__ import annotations

import json

import frappe

_WORKSPACE = "Ecommerce Integrations"
_REQUIRED_CHARTS = (
    "Sync Runs (Daily)",
    "Items Synced (Daily)",
    "AI Descriptions Generated (Daily)",
)


def execute() -> None:
    if not frappe.db.exists("DocType", "Workspace"):
        return
    if not frappe.db.exists("Workspace", _WORKSPACE):
        return

    # All three charts must be in place before we link them — otherwise
    # the workspace render would show a broken-chart placeholder.
    missing = [
        name for name in _REQUIRED_CHARTS
        if not frappe.db.exists("Dashboard Chart", name)
    ]
    if missing:
        frappe.logger().info(
            f"add_dashboard_charts_to_workspace: skipping — "
            f"chart(s) not yet synced: {missing}",
        )
        return

    ws = frappe.get_doc("Workspace", _WORKSPACE)
    try:
        blocks = json.loads(ws.content or "[]")
    except (ValueError, TypeError):
        return

    present = {
        b.get("data", {}).get("chart_name")
        for b in blocks if b.get("type") == "chart"
    }
    if all(name in present for name in _REQUIRED_CHARTS):
        return

    # Replace the first ``spacer`` block (``sp1``) with the trends
    # section — placed between the Number-Card row and the existing
    # Product/Catalog header so the layout reads top-to-bottom: KPIs →
    # trends → operations.
    new_blocks = []
    inserted = False
    for b in blocks:
        new_blocks.append(b)
        if not inserted and b.get("id") == "sp1":
            inserted = True
            new_blocks.extend([
                {
                    "id": "header_trends",
                    "type": "header",
                    "data": {
                        "text": "<span class=\"h4\"><b>Trends &amp; Volumes</b></span>",
                        "col": 12,
                    },
                },
                {
                    "id": "chart_runs",
                    "type": "chart",
                    "data": {
                        "chart_name": "Sync Runs (Daily)",
                        "col": 6,
                    },
                },
                {
                    "id": "chart_items",
                    "type": "chart",
                    "data": {
                        "chart_name": "Items Synced (Daily)",
                        "col": 6,
                    },
                },
                {
                    "id": "chart_ai",
                    "type": "chart",
                    "data": {
                        "chart_name": "AI Descriptions Generated (Daily)",
                        "col": 12,
                    },
                },
                {
                    "id": "sp_trends",
                    "type": "spacer",
                    "data": {"col": 12},
                },
            ])

    if not inserted:
        # Couldn't find the anchor — workspace layout has diverged.
        # Leave the existing layout alone rather than mangle it.
        return

    # Use ``frappe.db.set_value`` rather than ``ws.save()`` — the
    # workspace row in older installs may be missing mandatory fields
    # (``type``, ``parent_page``) that the v16 doctype now requires.
    # Going through set_value bypasses the doctype validator while
    # still writing through the standard cache invalidation path.
    update: dict[str, str] = {"content": json.dumps(new_blocks)}
    # Same patch repairs the missing ``type`` value that prevents the
    # workspace from being indexed by the Awesome Bar / global search
    # on installs that predate the v16 schema bump.
    if not frappe.db.get_value("Workspace", _WORKSPACE, "type"):
        update["type"] = "Workspace"
    frappe.db.set_value(
        "Workspace", _WORKSPACE, update,
        update_modified=True,
    )
    _ensure_chart_child_rows()
    # Full cache clear, not just ``doctype="Workspace"`` — Frappe v16
    # caches the ``get_desktop_page`` payload separately (per-user
    # cache keys + a global app-level cache), and only the unscoped
    # ``frappe.clear_cache()`` invalidates both layers. Without this
    # the SPA keeps serving the pre-patch payload with zero chart
    # blocks even though the child-table rows exist in the DB.
    frappe.clear_cache()
    frappe.db.commit()  # noqa: SLF001


def _ensure_chart_child_rows() -> None:
    """Populate the ``tabWorkspace Chart`` child table.

    Frappe v16 renders dashboard charts on a workspace from the
    ``charts`` child table — not from the ``chart`` blocks in the
    ``content`` JSON. The number-card child table is populated by the
    workspace import on first install, but ``charts`` lands as an
    empty array in older fixtures, so we need to insert the link
    rows ourselves.

    Each insert uses ``frappe.get_doc`` so v16's standard hooks fire
    (no shortcut via raw SQL). ``parent_doctype`` and ``parenttype``
    are set explicitly because the doctype enforces both.
    """
    existing = {
        row["chart_name"]
        for row in frappe.get_all(
            "Workspace Chart",
            filters={"parent": _WORKSPACE, "parenttype": "Workspace"},
            fields=["chart_name"],
        )
    }
    max_idx = frappe.db.sql(
        """SELECT COALESCE(MAX(idx), 0) FROM `tabWorkspace Chart`
           WHERE parent=%s AND parenttype='Workspace'""",
        (_WORKSPACE,),
    )[0][0] or 0
    for offset, name in enumerate(_REQUIRED_CHARTS, start=1):
        if name in existing:
            continue
        row = frappe.get_doc({
            "doctype": "Workspace Chart",
            "chart_name": name,
            "label": name,
            "parent": _WORKSPACE,
            "parenttype": "Workspace",
            "parentfield": "charts",
            "idx": max_idx + offset,
        })
        row.flags.ignore_permissions = True
        row.insert()
