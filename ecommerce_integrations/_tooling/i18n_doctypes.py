"""Convert German ``label`` and ``description`` fields in doctype JSONs.

Frappe auto-translates these fields via ``translations/<lang>.csv``
without needing developer markup. For Path B (English source), we
need to ensure the JSON values are English; the German originals get
copied to the ``de.csv`` so German operators still see the original
text.

Walks every ``*.json`` under ``ecommerce_integrations/`` that
contains ``"doctype": "DocType"``, ``"DocType"`` workspace docs, or
``"Number Card"`` / ``"Dashboard Chart"`` fixtures. Touches three
keys per field: ``label``, ``description``, and the top-level
``label`` of the doctype itself.

Run from the repo root:

    python ecommerce_integrations/_tooling/i18n_doctypes.py [--apply]
"""

from __future__ import annotations

import csv
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CSV_PATH = ROOT / "translations" / "de.csv"

# Pattern to spot German strings (same set the JS/PY audit uses).
GERMAN_MARKERS = ("ä", "ö", "ü", "Ä", "Ö", "Ü", "ß")
GERMAN_KEYWORDS = (
    "einstellung", "aktivieren", "speichern", "löschen", "vorschau",
    "fehler", "erfolg", "auswahl", "verknüp", "beschreibung", "kategor",
    "neu laden", "abbrechen", "verwerf", "übersetz", "ausführ",
    "fortgeschritten", "verfügbar", "übersicht", "bestätig", "warnung",
    "zurück", "weiter", "wurzel", "regel", "vorlage", "zeitplan",
    "schließen", "öffnen", "anlegen", "ausgewählt", "verfeinerung",
    "trag", "geh", "lade", "über", "läuft",
)

# Fields we want to translate in each JSON node.
TRANSLATABLE_KEYS = ("label", "description")


def is_german(text: str) -> bool:
    if not isinstance(text, str) or not text.strip():
        return False
    if any(ch in text for ch in GERMAN_MARKERS):
        return True
    low = text.lower()
    return any(kw in low for kw in GERMAN_KEYWORDS)


def walk(node, results: list[tuple[list, str]], path: list[str]) -> None:
    """Recurse the JSON tree; collect (path, value) for German strings."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k in TRANSLATABLE_KEYS and is_german(v):
                results.append((path + [k], v))
            walk(v, results, path + [k])
    elif isinstance(node, list):
        for i, item in enumerate(node):
            walk(item, results, path + [str(i)])


def collect() -> dict[Path, list[tuple[list, str]]]:
    """Return ``{json_path: [(field_path, value), ...]}``."""
    out: dict[Path, list[tuple[list, str]]] = {}
    for fp in ROOT.rglob("*.json"):
        if any(x in str(fp) for x in ("/__pycache__/", "/node_modules/")):
            continue
        try:
            doc = json.loads(fp.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(doc, dict):
            continue
        # Only look at Frappe doctype-shaped JSONs (have a "doctype" key
        # OR are inside a doctype/<name>/ folder).
        is_doctype_shaped = (
            "doctype" in doc or "DocType" in str(fp.parent.parent)
            or "/workspace/" in str(fp) or "/number_card/" in str(fp)
            or "/dashboard_chart/" in str(fp)
        )
        if not is_doctype_shaped:
            continue
        results: list[tuple[list, str]] = []
        walk(doc, results, [])
        if results:
            out[fp] = results
    return out


def main() -> int:
    apply = "--apply" in sys.argv
    findings = collect()
    total = sum(len(v) for v in findings.values())
    print(f"Found {total} German strings across {len(findings)} JSON files.")

    if not findings:
        return 0

    if not apply:
        list_all = "--list-all" in sys.argv
        for fp, items in findings.items():
            rel = fp.relative_to(ROOT)
            print(f"\n  {rel}")
            display_items = items if list_all else items[:5]
            for path, value in display_items:
                print(f"    {'.'.join(path)}: {value!r}")
            if not list_all and len(items) > 5:
                print(f"    … and {len(items) - 5} more")
        print("\n(re-run with --apply once an English-source map is built)")
        return 0

    # When --apply is set, expect the operator-supplied map in
    # i18n_doctype_map.py.
    try:
        from ecommerce_integrations._tooling.i18n_doctype_map import MAP
    except ImportError:
        print(
            "i18n_doctype_map.py missing — run without --apply first, "
            "then add MAP for the values you want converted.",
        )
        return 1

    # Apply by simple string replace on the JSON serialisation. This
    # avoids losing key order (Frappe is sensitive to field_order) and
    # is safe because doctype JSON values are unique enough that we
    # don't accidentally hit unrelated keys.
    csv_rows: list[tuple[str, str]] = []
    if CSV_PATH.exists():
        with CSV_PATH.open(encoding="utf-8", newline="") as f:
            for r in csv.reader(f):
                if len(r) >= 2:
                    csv_rows.append((r[0], r[1]))
    csv_keys = {k for (k, _v) in csv_rows}

    replaced_files = 0
    replaced_strings = 0
    for fp, items in findings.items():
        text = fp.read_text(encoding="utf-8")
        new_text = text
        changed = False
        for path, de_value in items:
            en_value = MAP.get(de_value)
            if not en_value:
                continue
            # Replace as JSON-quoted strings (handle escape edge cases by
            # round-tripping through json.dumps).
            de_quoted = json.dumps(de_value, ensure_ascii=False)
            en_quoted = json.dumps(en_value, ensure_ascii=False)
            if de_quoted in new_text:
                new_text = new_text.replace(de_quoted, en_quoted)
                changed = True
                replaced_strings += 1
                if en_value not in csv_keys:
                    csv_rows.append((en_value, de_value))
                    csv_keys.add(en_value)
        if changed:
            fp.write_text(new_text, encoding="utf-8")
            replaced_files += 1

    csv_rows.sort(key=lambda r: r[0].lower())
    with CSV_PATH.open("w", encoding="utf-8", newline="") as f:
        csv.writer(f).writerows(csv_rows)

    print(
        f"\nReplaced {replaced_strings} strings across {replaced_files} files. "
        f"CSV now has {len(csv_rows)} rows.",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
