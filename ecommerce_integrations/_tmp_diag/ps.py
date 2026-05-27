"""Property classification + sort/hide preview.

DRY-RUN by default — prints what would change without writing.
Run with main(apply="yes") to actually update."""

import re

# Category priorities (lower = appears first on PDP).
# Each property name is classified by:
#   1. exact match in CATEGORIES (highest priority match wins)
#   2. prefix/contains pattern
#   3. default 500
CATEGORIES = [
    # (priority, regex pattern, label)
    (100, r"^(Breite|Länge|Höhe|Tiefe|Gewicht|Maße)(\s*\(mm\))?$", "Hauptmaße"),
    (110, r"^Gesamt(breite|länge|höhe|tiefe)", "Gesamtmaße"),
    (120, r"^(Lichte|Innen|Außen)\s+(Breite|Länge|Höhe|Tiefe)", "Innen-/Außenmaße"),
    (150, r"bereich$", "Maße-Bereich"),  # Breitenbereich, Höhenbereich
    (200, r"^(Material|Oberfläche|Bauweise|Konstruktion)$", "Material/Bauweise"),
    (220, r"^Regal(system|tiefe|art|typ)?$", "Regalsystem"),
    (250, r"^(Tragkraft|Feldlast|Belastung|Anzahl)\s|^Anzahl\s|^Tragkraft\s", "Kapazität"),
    (260, r"^Schubladen?\s", "Schubladen"),
    (270, r"^Verstellraster$|^Auflage", "Verstellbarkeit"),
    (300, r"^(Grundfarbe|Farbton|Farbe)\b|farbe$", "Farbe"),
    (350, r"^Front(höhe|farbe)|^Korpus", "Front/Korpus"),
    (400, r"^(Ausstattung|Schliessung|Zugriff)", "Ausstattung"),
    (450, r"^Rahmen\s|^Auslieferungs|^Lieferzustand", "Lieferzustand"),
    (700, r"^Aufnahme[- ]", "Aufnahme-Specs"),
    (900, r"^Versand[- ]", "Versand-Specs"),
]
DEFAULT_PRIORITY = 500

# Values that get hidden (set sync_to_shopware=0).
HIDE_VALUES = {"false", "0", "no", "nein", "n", "-", "", "n/a", "none", "null"}


def classify(name: str) -> tuple[int, str]:
    """Return (priority, category_label) for a property name."""
    for prio, pattern, label in CATEGORIES:
        if re.search(pattern, name, re.IGNORECASE):
            return prio, label
    return DEFAULT_PRIORITY, "Sonstige"


def main(apply: str = "no"):
    import frappe
    do_apply = (apply == "yes")

    # 1. Distinct property names → classify + count
    rows = frappe.db.sql("""
      SELECT property_name, COUNT(*) cnt FROM `tabItem Ecommerce Property`
      WHERE parenttype='Item' AND sync_to_shopware=1
      GROUP BY property_name
    """, as_dict=True)
    print(f"=== {len(rows)} distinct active property names ===")
    by_cat: dict = {}
    for r in rows:
        prio, label = classify(r['property_name'])
        by_cat.setdefault(label, []).append((prio, r['property_name'], r['cnt']))
    for label in sorted(by_cat.keys(), key=lambda l: by_cat[l][0][0]):
        items = sorted(by_cat[label])
        sample = ", ".join(n for _,n,_ in items[:3])
        total = sum(c for _,_,c in items)
        print(f"  [{items[0][0]:3}] {label:25}  {len(items):>3} names, {total:>6} rows — {sample}")

    # 2. Hide rules: false/null values
    hide_q = """
      SELECT COUNT(*) FROM `tabItem Ecommerce Property`
      WHERE parenttype='Item' AND sync_to_shopware=1
        AND (LOWER(TRIM(IFNULL(property_value,''))) IN %(vals)s)
    """
    hide_count = frappe.db.sql(hide_q, {"vals": tuple(HIDE_VALUES)})[0][0]
    print(f"\n=== HIDE RULE: false/null values ===")
    print(f"  rows to hide: {hide_count}")

    # 3. Dedup Versand-X = X
    print(f"\n=== HIDE RULE: Versand-X with same value as X ===")
    dedupe = frappe.db.sql("""
      SELECT v.parent, v.property_name vname, v.property_value vval, b.property_name bname
      FROM `tabItem Ecommerce Property` v
      JOIN `tabItem Ecommerce Property` b
        ON b.parent=v.parent AND b.parenttype=v.parenttype
        AND b.property_name = REPLACE(v.property_name, 'Versand-', '')
        AND b.property_value = v.property_value
      WHERE v.parenttype='Item' AND v.property_name LIKE 'Versand-%' AND v.sync_to_shopware=1
    """, as_dict=True)
    print(f"  Versand-X dedup hits: {len(dedupe)}")

    if not do_apply:
        print("\nDRY RUN — re-run with apply='yes' to commit.")
        return

    # ===== APPLY =====
    print("\n=== APPLYING ===")
    # Hide false/null
    n = frappe.db.sql("""
      UPDATE `tabItem Ecommerce Property` SET sync_to_shopware=0
      WHERE parenttype='Item' AND sync_to_shopware=1
        AND (LOWER(TRIM(IFNULL(property_value,''))) IN %(vals)s)
    """, {"vals": tuple(HIDE_VALUES)})
    frappe.db.commit()
    print(f"hidden false/null: {n} rows touched")
    
    # Hide Versand-X dedupes
    for d in dedupe:
        frappe.db.set_value(
            "Item Ecommerce Property",
            {"parent": d['parent'], "parenttype": "Item",
             "property_name": d['vname'], "property_value": d['vval']},
            "sync_to_shopware", 0, update_modified=False,
        )
    frappe.db.commit()
    print(f"hidden Versand-dedupes: {len(dedupe)}")
    
    # Re-order by priority — update idx based on classifier
    # Walk per-parent (because idx is per-parent)
    parents = frappe.db.sql("""
      SELECT DISTINCT parent FROM `tabItem Ecommerce Property`
      WHERE parenttype='Item'
    """, pluck="parent")
    print(f"reordering {len(parents)} parents...")
    touched = 0
    for parent in parents:
        rows = frappe.db.sql("""
          SELECT name, property_name FROM `tabItem Ecommerce Property`
          WHERE parent=%s AND parenttype='Item' ORDER BY idx
        """, (parent,), as_dict=True)
        # Sort by (priority, name) and reassign idx
        rows.sort(key=lambda r: (classify(r['property_name'])[0], r['property_name']))
        for new_idx, r in enumerate(rows, start=1):
            frappe.db.set_value("Item Ecommerce Property", r['name'], "idx", new_idx, update_modified=False)
            touched += 1
    frappe.db.commit()
    print(f"reordered: {touched} rows")
    print("DONE.")
