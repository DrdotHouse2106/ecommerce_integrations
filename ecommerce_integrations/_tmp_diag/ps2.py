"""Property classification v2 — better coverage of the 'Sonstige' bucket."""

import re

CATEGORIES = [
    # Hauptmaße (echte Produktdimensionen)
    (100, r"^(Breite|Länge|Höhe|Tiefe|Gewicht|Maße|Durchmesser|Innendurchmesser)(\s*\(mm\))?$"),
    (105, r"^(Bauhöhe|Hubhöhe|Hubbereich|Schreibhöhe|Vorbaumaß|Auslaufhöhe|Ausschütthöhe|Höhe pro Hub|Ausfahrhöhe|Kettenlänge|Nabenlänge|Gabellänge|Gabelbreite|Tragbreite|Bohrungs-Ø|Schraubloch)"),
    (108, r"^(Höhe|Tiefe|Breite)\s+(min|max|ohne|mit|inkl)|^Höhe\s+der"),
    (110, r"^Gesamt(breite|länge|höhe|tiefe|tragkraft|gewicht)"),
    (115, r"^(Innen|Außen|Lichte)\s*(breite|länge|höhe|tiefe|fach|maße)|^Innen(breite|höhe|länge)"),
    (117, r"^(Standfläche|Nutzfläche|Stellfläche|Palettenbreite|Innenmaße|Kastenmaße)"),
    (130, r"^Lade(flächen)?(länge|breite|höhe)|^Etagenhöhen|^Fach(breite|höhe|last)"),
    (135, r"^(Mulden|Schaufel|Trichter)"),
    (140, r"^(Spannbereich|Hakenabstand|Höhe der Auffangwanne|Breite der Auffangwanne|Tiefe der Auffangwanne)"),
    (150, r"bereich$|^Spannbereich"),
    
    # Material & Bauweise
    (200, r"^(Material|Oberfläche|Bauweise|Konstruktion|Bodentyp|Bodenart|Türmaterial|Plattenstärke|Arbeitsplatte|Aussteifungsart|Rost|RAL-Nummer|Material\s+(der|Arbeitsplatte))"),
    (210, r"^(Aufstellung|Montageart|Türausführung|Spritzschutzwand|PE-Einsatz)$"),
    
    # Regal / system / typ
    (220, r"^(Regal(system|tiefe|art|typ)?|System|Typ|Ausführung|Modul|Grund-/Anbauregal|Bauweise|Felge|Bereifung|Radlagerung)"),
    
    # Kapazität / Tragkraft / Belastung / Anzahl
    (250, r"^(Tragkraft|Tragfähigkeit|Feldlast|Belastung|Belastbarkeit|Belastb|Fachlast|Auflast|Radlast|Flächenlast|Stützlast|Zuggewicht|Zulässiges|Förderleistung|Maximale Streubreite|Maximale Dichte|Max\.|Maximale)\b"),
    (256, r"Anzahl|^Maximale?\s+Anzahl"),
    (260, r"^Schubladen?\s|^Anzahl Schubladen"),
    (270, r"^Verstellraster$|^Auflage|^Verstellbar|^Höhenverstellbar"),
    (275, r"^(Inhalt|Auffangvolumen|Volumen|Mindestölbedarf)"),
    
    # Räder / Mobilität
    (280, r"^(Bereifung|Radgröße|Radtyp|Radlagerung|Felge|Fahrbar|Rangierhilfe|Schiebegriff|Nabenlänge|Klappenöffnungswinkel|Kippvorgang|Kippwinkel|Neigungswinkel|Zulässige Personenzahl)"),
    
    # Farbe
    (300, r"^(Grundfarbe|Farbton|Farbe|Sekundär-Farbton|Korpusfarbe|Türfarbe|Farbkombination)\b|farbe$"),
    (350, r"^Front(höhe|farbe)|^Korpus"),
    
    # Ausstattung / Schließung
    (400, r"^(Ausstattung|Schliessung|Schließung|Zugriff|Zubehör|Spritzschutz)"),
    
    # Lieferzustand
    (450, r"^(Rahmen|Auslieferungs|Lieferzustand)"),
    
    # Anwendung / Eignung
    (500, r"^(Geeignet\s+für|Eignung|Fasslagerung|Schildhöhe|Schildbreite|Einstellbare\s+Räumbreiten|Technische\s+Eigenschaft|Fass\s+(Durchmesser|Höhe)|Gabelzinken|Gabelrolle|Schreibhöhe|Schaufel|Material\s+der\s+Ladefläche|Höhe der Flaschenhalterung|Schüttkantenhöhe|Stellfläche\s+(Breite|Länge)|Lichte\s+Fachbreite)"),
    
    # Sicherheit / Zulassung / Doku
    (600, r"^(DIBt-Zulassung|UN-Zulassung|Richtlinie|Hinweis)"),
    (610, r"^(Produktblätter|Sicherheitsblätter|Datenblatt)"),
    
    # Aufnahme (legacy)
    (700, r"^Aufnahme[- ]"),
    
    # Versand (legacy)
    (900, r"^Versand[- ]"),
]
DEFAULT_PRIORITY = 800  # "Sonstige" lands later

# Hard hide — marketing slogans + internal flags
HIDE_NAMES = {
    "is_mehrpreis", "is_surcharge", "Übersichtlichkeit",
    "Zentrale Sicherheit", "Höchste Flexibilität", "Platzsparend",
    "Perfekte Übersichtlichkeit", "Robust und langlebig",
    "Modular erweiterbar", "Maximale Mobilität",
    "Sicherheit & Stabilität", "Sicher & geprüft",
    "Klare, einseitige Organisation", "6 Lochplatten",
    "Höhe Maximum",
}
HIDE_VALUES = {"false", "0", "no", "nein", "n", "-", "", "n/a", "none", "null"}


def classify(name):
    for prio, p in CATEGORIES:
        if re.search(p, name, re.IGNORECASE):
            return prio
    return DEFAULT_PRIORITY


def main(apply: str = "no"):
    import frappe
    do_apply = (apply == "yes")
    rows = frappe.db.sql("""
      SELECT property_name, COUNT(*) cnt FROM `tabItem Ecommerce Property`
      WHERE parenttype='Item' AND sync_to_shopware=1
      GROUP BY property_name
    """, as_dict=True)
    
    # bucket counts
    buckets = {}
    sonst_names = []
    for r in rows:
        if r['property_name'] in HIDE_NAMES:
            buckets.setdefault(("HIDE_NAME","hard-hide"), []).append(r)
            continue
        p = classify(r['property_name'])
        buckets.setdefault((p, "cat"), []).append(r)
        if p == DEFAULT_PRIORITY:
            sonst_names.append(r)
    
    # report
    for (k, label), items in sorted(buckets.items(), key=lambda kv: (kv[0][0] if kv[0][0]!="HIDE_NAME" else 9999)):
        total = sum(r['cnt'] for r in items)
        sample = ", ".join(r['property_name'] for r in items[:3])
        print(f"  [{k:>10}] {label:12} {len(items):>3} names, {total:>6} rows — {sample}")
    
    print(f"\nNoch in 'Sonstige' (priority {DEFAULT_PRIORITY}):  {len(sonst_names)} names, {sum(r['cnt'] for r in sonst_names)} rows")
    if sonst_names:
        for r in sonst_names[:20]:
            print(f"  {r['cnt']:>5}x  {r['property_name']}")
    
    # hide rules
    hide_count = frappe.db.sql("""
      SELECT COUNT(*) FROM `tabItem Ecommerce Property`
      WHERE parenttype='Item' AND sync_to_shopware=1
        AND ( LOWER(TRIM(IFNULL(property_value,''))) IN %(vals)s
              OR property_name IN %(names)s )
    """, {"vals": tuple(HIDE_VALUES), "names": tuple(HIDE_NAMES) or ("__none__",)})[0][0]
    print(f"\n=== HIDE TOTAL: {hide_count} rows (false/null + marketing-slogans + internal flags) ===")
    
    if not do_apply:
        print("\nDRY RUN — re-run with apply='yes' to commit.")
        return
    
    # APPLY
    n = frappe.db.sql("""
      UPDATE `tabItem Ecommerce Property` SET sync_to_shopware=0
      WHERE parenttype='Item' AND sync_to_shopware=1
        AND ( LOWER(TRIM(IFNULL(property_value,''))) IN %(vals)s
              OR property_name IN %(names)s )
    """, {"vals": tuple(HIDE_VALUES), "names": tuple(HIDE_NAMES) or ("__none__",)})
    frappe.db.commit()
    print(f"hidden: {hide_count}")
    
    # Reorder
    parents = frappe.db.sql("""
      SELECT DISTINCT parent FROM `tabItem Ecommerce Property` WHERE parenttype='Item'
    """, pluck="parent")
    print(f"reordering {len(parents)} parents...")
    for parent in parents:
        rs = frappe.db.sql("""
          SELECT name, property_name FROM `tabItem Ecommerce Property`
          WHERE parent=%s AND parenttype='Item'
        """, (parent,), as_dict=True)
        rs.sort(key=lambda r: (classify(r['property_name']), r['property_name']))
        for new_idx, r in enumerate(rs, start=1):
            frappe.db.set_value("Item Ecommerce Property", r['name'], "idx", new_idx, update_modified=False)
    frappe.db.commit()
    print("DONE.")
