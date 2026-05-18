"""Collapse rarely-edited Settings sections by default.

Both Shopware Setting and Medusa Setting forms ship with 20–26 open
sections, which is too much for a first-time operator to scan. This
script flips ``collapsible: 1`` on the sections that are typically
configured once and then ignored, leaving the daily-use sections open.

Idempotent: re-running the script with no changes is a no-op.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Mapping: section label (post-i18n English) → desired ``collapsible`` (0/1)
# Day-1-essential sections stay open (0); set-once / advanced sections
# collapse (1). The labels here match the post-i18n English values.
SHOPWARE_SECTIONS = {
    "Storefronts (sales channels)": 1,
    "Category sync (ERP → shop)": 1,
    "Eigene Kollektionen (Sale, Bestseller, …)": 1,
    "Produkt-Sync (ERP → Shop)": 0,           # short — keep open, it's just a link
    "Pull Sync (Shop → ERP)": 0,              # same
    "Full sync (advanced)": 1,
    "Massen-Synchronisation (Performance)": 1,
    "Verbindung": 0,                          # essential
    "Integration credentials (Shopware Admin > Settings > System > Integrations)": 0,
    "User credentials (Shopware Admin > Settings > System > Users)": 1,
    "Webhooks (incoming events)": 1,
    "Kunden": 0,
    "Firma & Buchhaltung": 0,
    "Preise (B2B / B2C)": 1,
    "Bestellungen (Eingang aus Shopware)": 1,
    "Zahlungsart-Zuordnung": 1,
    "MwSt.-Konten": 1,
    "Produkt-Upload (ERPNext → Shopware)": 1, # superseded by Product Sync
    "Versandkosten": 1,
    "Category sync (legacy settings)": 1,
    "Batch sizes for the full sync": 1,
    "Field mappings (advanced)": 1,
    "Bestand (Lager-Sync)": 1,
    "Checkout fields (advanced)": 1,
}

MEDUSA_SECTIONS = {
    "Verbindung": 0,
    "Webhooks (incoming events)": 1,
    "Storefronts (sales channels)": 1,
    "Category sync (ERP → shop)": 1,
    "Eigene Kollektionen (Sale, Bestseller, …)": 1,
    "Produkt-Sync (ERP → Shop)": 0,
    "Pull Sync (Shop → ERP)": 0,
    "Kunden": 0,
    "Firma & Buchhaltung": 0,
    "Preise (B2B / B2C)": 1,
    "Bestellungen (Eingang aus Medusa)": 1,
    "Beleg-Automatisierung": 1,
    "Zahlungs-Sync": 1,
    "Zahlungsart-Zuordnung": 1,
    "Steuern": 1,
    "Produkt-Upload (ERPNext → Medusa)": 1,
    "Bestand (Lager-Sync)": 1,
    "Lager-Zuordnungen (Multi-Lager)": 1,
    "Versand & Rabatte": 1,
    "Checkout fields (advanced)": 1,
}

TARGETS = [
    (ROOT / "shopware6/doctype/shopware_setting/shopware_setting.json",
     SHOPWARE_SECTIONS),
    (ROOT / "medusa/doctype/medusa_setting/medusa_setting.json",
     MEDUSA_SECTIONS),
]


def apply(path: Path, sections: dict[str, int]) -> int:
    """Set ``collapsible`` on matching Section Break fields. Returns changes done."""
    doc = json.loads(path.read_text(encoding="utf-8"))
    fields = doc.get("fields", [])
    changes = 0
    for f in fields:
        if f.get("fieldtype") != "Section Break":
            continue
        label = f.get("label")
        if label not in sections:
            continue
        target = sections[label]
        current = int(f.get("collapsible") or 0)
        if current != target:
            if target:
                f["collapsible"] = 1
            else:
                # Remove the key entirely when target=0 to keep the JSON
                # minimal (Frappe treats absence as not-collapsible).
                f.pop("collapsible", None)
            changes += 1
    if changes:
        path.write_text(
            json.dumps(doc, indent=1, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    return changes


def main() -> int:
    total = 0
    for path, sections in TARGETS:
        n = apply(path, sections)
        print(f"  {path.relative_to(ROOT)}: {n} sections updated")
        total += n
    print(f"Total: {total} changes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
