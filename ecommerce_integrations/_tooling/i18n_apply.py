"""Apply ``i18n_de_to_en.MAP`` to the source tree.

For each ``german_source → english_source`` entry:

1. Find every ``_('german_source')`` / ``__('german_source')`` in
   ``.py`` and ``.js`` files (using the same patterns as
   ``i18n_audit.py``).
2. Replace the literal with the English source — preserving the
   surrounding quote character (single vs double).
3. Add or update a row in ``translations/de.csv`` mapping
   ``english_source → german_source`` so German operators still see
   the original copy.

Idempotent: running twice changes nothing on the second pass because
the German strings are no longer in the source tree.

Run from the repo root:

    python ecommerce_integrations/_tooling/i18n_apply.py [--dry-run]
"""

from __future__ import annotations

import csv
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CSV_PATH = ROOT / "translations" / "de.csv"

from ecommerce_integrations._tooling.i18n_de_to_en import MAP  # noqa: E402


def replace_in_file(fp: Path, mapping: dict[str, str]) -> int:
    """Rewrite ``_('de')`` → ``_('en')`` in one file. Returns replacements done."""
    try:
        text = fp.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return 0
    original = text
    total = 0
    for de, en in mapping.items():
        # Escape special regex chars in the source (e.g. parens, dots).
        de_escaped = re.escape(de)
        # Replace within single- and double-quoted literals.
        for quote in ("'", '"'):
            # Anchor on a non-identifier char before ``_(`` so we don't catch
            # ``my_helper_(...)``. JS ``__(`` is also matched.
            pat = re.compile(
                r"(__\(\s*|(?<![a-zA-Z_])_\(\s*)" + quote + de_escaped + quote,
            )
            new_text, n = pat.subn(
                lambda m, en=en, q=quote: m.group(1) + q + en + q,
                text,
            )
            text = new_text
            total += n
    if text != original:
        fp.write_text(text, encoding="utf-8")
    return total


def update_csv(mapping: dict[str, str]) -> int:
    """Add english_source → german_source rows. Returns rows added."""
    rows: list[tuple[str, str]] = []
    if CSV_PATH.exists():
        with CSV_PATH.open(encoding="utf-8", newline="") as f:
            for row in csv.reader(f):
                if len(row) >= 2:
                    rows.append((row[0], row[1]))
    existing_keys = {k for (k, _v) in rows}
    added = 0
    for de, en in mapping.items():
        if en in existing_keys:
            continue
        rows.append((en, de))
        existing_keys.add(en)
        added += 1
    rows.sort(key=lambda r: r[0].lower())
    with CSV_PATH.open("w", encoding="utf-8", newline="") as f:
        csv.writer(f).writerows(rows)
    return added


def main() -> int:
    dry_run = "--dry-run" in sys.argv

    if dry_run:
        print(f"DRY RUN — would convert {len(MAP)} German strings")
        return 0

    total = 0
    for fp in list(ROOT.rglob("*.js")) + list(ROOT.rglob("*.py")):
        if any(x in str(fp) for x in ("/node_modules/", "/__pycache__/", "/_tooling/")):
            continue
        total += replace_in_file(fp, MAP)

    added = update_csv(MAP)
    print(f"Replaced {total} call-sites; added {added} CSV rows.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
