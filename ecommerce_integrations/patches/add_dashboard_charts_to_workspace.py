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
    frappe.clear_cache(doctype="Workspace")
    frappe.db.commit()  # noqa: SLF001
