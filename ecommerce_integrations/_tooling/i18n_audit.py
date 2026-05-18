"""Inventory the codebase's translation state.

Walks every ``.js`` and ``.py`` file under ``ecommerce_integrations/``,
extracts every literal string passed to ``__('...')`` (JS) or ``_('...')``
(Python), and bucketises against the shipped ``translations/de.csv``.

Three output buckets so the conversion work can be sliced:

- **already_translated**: source string is English AND has a de.csv
  row → nothing to do.
- **english_no_translation**: source looks English but has no de.csv
  row → may need a German row added (operators with `lang=de` would
  see English fallback otherwise).
- **german_source**: source is in German → must be converted to
  English in the source file, AND added to de.csv with the German
  as translation target.

Run from the repo root:

    python ecommerce_integrations/_tooling/i18n_audit.py
"""

from __future__ import annotations

import csv
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CSV_PATH = ROOT / "translations" / "de.csv"

# Patterns: capture the string literal AND remember the file/line for
# reverse-lookup. We anchor on a non-identifier char before ``_(`` so
# we don't catch ``my_helper_(...)``.
JS_PATTERN = re.compile(r"__\(\s*(['\"])([^'\"\\]+?)\1")
PY_PATTERN = re.compile(r"(?:^|[^a-zA-Z_])_\(\s*(['\"])([^'\"\\]+?)\1")

GERMAN_MARKERS = ("ä", "ö", "ü", "Ä", "Ö", "Ü", "ß")
GERMAN_KEYWORDS = (
    "einstellung", "aktivieren", "speichern", "löschen", "vorschau",
    "fehler", "erfolg", "auswahl", "verknüp", "beschreibung", "kategor",
    "neu laden", "abbrechen", "verwerf", "übersetz", "ausführ", "fortgeschritten",
    "verfügbar", "übersicht", "bestätig", "warnung", "zurück", "weiter",
)


def is_german(text: str) -> bool:
    if any(ch in text for ch in GERMAN_MARKERS):
        return True
    low = text.lower()
    return any(kw in low for kw in GERMAN_KEYWORDS)


def collect_strings() -> dict[str, list[tuple[Path, int]]]:
    """Return ``{string: [(path, line), ...]}`` for every wrapped string."""
    out: dict[str, list[tuple[Path, int]]] = defaultdict(list)

    for fp in ROOT.rglob("*.js"):
        if "/node_modules/" in str(fp) or "/dist/" in str(fp):
            continue
        try:
            text = fp.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            for m in JS_PATTERN.finditer(line):
                out[m.group(2)].append((fp, lineno))

    for fp in ROOT.rglob("*.py"):
        if "/__pycache__/" in str(fp):
            continue
        try:
            text = fp.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            for m in PY_PATTERN.finditer(line):
                out[m.group(2)].append((fp, lineno))

    return out


def load_translations() -> dict[str, str]:
    if not CSV_PATH.exists():
        return {}
    out: dict[str, str] = {}
    with CSV_PATH.open(encoding="utf-8", newline="") as f:
        for row in csv.reader(f):
            if len(row) >= 2:
                out[row[0]] = row[1]
    return out


def main() -> int:
    strings = collect_strings()
    translations = load_translations()

    already_translated: list[str] = []
    english_no_translation: list[str] = []
    german_source: dict[str, list[tuple[Path, int]]] = {}

    for s, locations in strings.items():
        if s in translations:
            already_translated.append(s)
        elif is_german(s):
            german_source[s] = locations
        else:
            english_no_translation.append(s)

    print(f"Unique wrapped strings    : {len(strings)}")
    print(f"  Already in de.csv       : {len(already_translated)}")
    print(f"  English w/o translation : {len(english_no_translation)}")
    print(f"  German source (needs conversion): {len(german_source)}")

    if "--list-german" in sys.argv:
        print("\n=== German source strings ===")
        for s, locs in sorted(german_source.items()):
            print(f"\n  {s!r}")
            for path, line in sorted(set(locs)):
                rel = path.relative_to(ROOT)
                print(f"    {rel}:{line}")

    if "--list-english-untranslated" in sys.argv:
        print("\n=== English source w/o de.csv entry ===")
        for s in sorted(english_no_translation):
            print(f"  {s!r}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
