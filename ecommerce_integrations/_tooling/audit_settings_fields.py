"""Find Setting-doctype fields that the codebase never reads.

For each ``fieldname`` defined in Shopware Setting and Medusa Setting,
grep the Python + JS source tree for actual references. Fields with
zero references are dead — they likely predate the Product Sync /
Pull Sync / Catalog Mirror SSOT split and can be removed (with a
migration that keeps the column alive for one release for safety).

The audit is deliberately permissive about references:
- ``self.fieldname`` / ``doc.fieldname`` / ``settings.fieldname``
- ``"fieldname"`` quoted in any ``frappe.db.get_value`` /
  ``frappe.get_doc`` / ``__onload`` access
- JS ``frm.doc.fieldname`` / ``cur_frm.doc.fieldname``

Run from the repo root:

    python -m ecommerce_integrations._tooling.audit_settings_fields
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


# DocType JSONs we want to audit + module-folder names where their code lives.
# A reference inside the doctype's own folder is "internal use".
TARGETS = [
    {
        "label": "Shopware Setting",
        "json": ROOT / "shopware6/doctype/shopware_setting/shopware_setting.json",
        "own_folders": ["shopware6"],
    },
    {
        "label": "Medusa Setting",
        "json": ROOT / "medusa/doctype/medusa_setting/medusa_setting.json",
        "own_folders": ["medusa"],
    },
]


def collect_field_refs(fieldname: str) -> dict[str, list[str]]:
    """Return ``{file_kind: [paths]}`` listing every file that references the field."""
    # Patterns that mean "this field is being read":
    #   .fieldname            (Python attribute / JS dot access)
    #   ['fieldname']         (Python dict / JS bracket)
    #   "fieldname"           (db.get_value('Shopware Setting', None, 'field'))
    #   "shopware_setting"    keyword check excluded; we use the field-side anchor
    # We anchor on the actual fieldname to avoid generic-keyword false positives.
    if re.search(r"^[a-z_][a-z0-9_]*$", fieldname) is None:
        return {}

    patterns = [
        rf"\.{fieldname}\b",          # .fieldname
        rf"['\"]{fieldname}['\"]",   # 'fieldname' or "fieldname"
        rf"\[{fieldname}\]",          # [fieldname] (rare, but happens)
    ]
    combined = re.compile("|".join(patterns))

    py_hits: list[str] = []
    js_hits: list[str] = []
    for fp in ROOT.rglob("*.py"):
        if any(x in str(fp) for x in ("/__pycache__/", "/_tooling/", "/tests/")):
            continue
        try:
            text = fp.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if combined.search(text):
            py_hits.append(str(fp.relative_to(ROOT)))
    for fp in ROOT.rglob("*.js"):
        if any(x in str(fp) for x in ("/node_modules/", "/dist/", "/_tooling/")):
            continue
        try:
            text = fp.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if combined.search(text):
            js_hits.append(str(fp.relative_to(ROOT)))
    return {"py": py_hits, "js": js_hits}


def audit(target: dict) -> None:
    doc = json.loads(target["json"].read_text(encoding="utf-8"))
    fields = doc.get("fields", [])
    own_folder_patterns = [f"/{ofp}/" for ofp in target["own_folders"]]

    # Layout fields aren't "fields" — exclude them.
    skip_types = {"Section Break", "Column Break", "Tab Break", "HTML", "Heading"}

    print(f"\n========== {target['label']} ==========")
    print(f"Total fields in JSON: {len(fields)}")

    rows = []
    for f in fields:
        if f.get("fieldtype") in skip_types:
            continue
        fname = f.get("fieldname")
        if not fname:
            continue
        refs = collect_field_refs(fname)
        py = refs.get("py", [])
        js = refs.get("js", [])
        all_hits = py + js
        own = [h for h in all_hits if any(p in "/" + h for p in own_folder_patterns)]
        external = [
            h for h in all_hits
            if not any(p in "/" + h for p in own_folder_patterns)
        ]
        # Exclude the doctype's own controller .py from "external" — it's
        # always part of "own". But also exclude pure i18n / tooling pickups
        # we already filtered.
        rows.append({
            "fieldname": fname,
            "fieldtype": f.get("fieldtype"),
            "label": f.get("label") or "",
            "py_refs": len(py),
            "js_refs": len(js),
            "own_refs": len(own),
            "external_refs": len(external),
            "all_hits": all_hits,
        })

    # 1. Truly unused (no references anywhere)
    unused = [r for r in rows if not r["all_hits"]]
    # 2. Only referenced inside doctype's own folder (potentially dead)
    only_own = [r for r in rows
                if r["all_hits"] and r["external_refs"] == 0
                and not _is_obvious_metadata(r)]

    print(f"\nUnused fields (no references anywhere): {len(unused)}")
    for r in unused:
        print(f"  {r['fieldname']:40s} {r['fieldtype']:12s} — {r['label']}")

    print(f"\nOnly-internal references (potentially dead): {len(only_own)}")
    for r in only_own:
        print(f"  {r['fieldname']:40s} {r['fieldtype']:12s} — {r['label']}")
        for h in r["all_hits"][:3]:
            print(f"      → {h}")
        if len(r["all_hits"]) > 3:
            print(f"      → … and {len(r['all_hits']) - 3} more")


def _is_obvious_metadata(r: dict) -> bool:
    """Skip fields that are pure metadata (will always have only-internal hits).

    These show up in the controller's own `.py` and JSON but aren't
    candidates for removal: state-tracking fields, autoname fields,
    custom fields the framework manages.
    """
    fname = r["fieldname"]
    if fname.startswith(("last_", "next_")):
        return True
    if fname.endswith(("_at", "_id", "_secret", "_token", "_password", "_key")):
        return True
    if r["fieldtype"] in ("Password", "Read Only"):
        return True
    return False


def main() -> int:
    for t in TARGETS:
        audit(t)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
