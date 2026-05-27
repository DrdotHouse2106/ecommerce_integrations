import re

CATEGORIES = [
    # Hauptmaße
    (100, r"^(Breite|Länge|Höhe|Tiefe|Gewicht|Maße|Durchmesser|Innendurchmesser)(\s*\(mm\))?$"),
    (105, r"^(Bauhöhe|Hubhöhe|Hubbereich|Schreibhöhe|Vorbaumaß|Auslaufhöhe|Ausschütthöhe|Höhe pro Hub|Ausfahrhöhe|Kettenlänge|Nabenlänge|Gabellänge|Gabelbreite|Tragbreite|Bohrungs|Schraubloch|Anschraubplatte|Flaschen-Durchmesser)"),
    (108, r"^(Höhe|Tiefe|Breite)\s+(min|max|ohne|mit|inkl|der)"),
    (110, r"^Gesamt(breite|länge|höhe|tiefe|tragkraft|gewicht)"),
    (115, r"^(Innen|Außen|Lichte)\s*(breite|länge|höhe|tiefe|fach|maße)|^Innen(breite|höhe|länge)"),
    (117, r"^(Standfläche|Nutzfläche|Stellfläche|Palettenbreite|Innenmaße|Kastenmaße|Scherenverschluss|Schieberverschluss)"),
    (130, r"^(Lade(flächen)?|Etagenhöhen|Fach|Abteil)(länge|breite|höhe|last)?"),
    (135, r"^(Mulden|Schaufel|Trichter)"),
    (140, r"^(Spannbereich|Hakenabstand|Auffangwanne|Maximaler Durchmesser|Minimaler Durchmesser)"),
    (150, r"bereich$|^Spannbereich"),
    
    # Material & Bauweise
    (200, r"^(Material|Oberfläche|Bauweise|Konstruktion|Bodentyp|Bodenart|Türmaterial|Plattenstärke|Arbeitsplatte|Aussteifungsart|Rost|RAL-Nummer|Material\s+(der|Arbeitsplatte))"),
    (210, r"^(Aufstellung|Montageart|Türausführung|Spritzschutzwand|PE-Einsatz)$"),
    
    # Regal / system / typ
    (220, r"^(Regal(system|tiefe|art|typ)?|System|Typ|Ausführung|Modul|Grund-/Anbauregal|Bauweise|Felge|Bereifung|Radlagerung)"),
    
    # Kapazität / Tragkraft / Belastung
    (250, r"^(Tragkraft|Tragfähigkeit|Feldlast|Belastung|Belastbarkeit|Belastb|Fachlast|Auflast|Radlast|Flächenlast|Stützlast|Zuggewicht|Zulässiges|Förderleistung|Streubreite|Maximale\s+(Streubreite|Dichte|Stützlast)|Max\.\s+\w|Stapelbar)"),
    (256, r"Anzahl|^Maximale?\s+Anzahl"),
    (260, r"^Schubladen?\s|^Anzahl Schubladen"),
    (270, r"^Verstellraster$|^Auflage|^Verstellbar|^Höhenverstellbar"),
    (275, r"^(Inhalt|Auffangvolumen|Volumen|Mindestölbedarf)"),
    
    # Räder / Mobilität
    (280, r"^(Bereifung|Radgröße|Radtyp|Radlagerung|Felge|Fahrbar|Rangierhilfe|Schiebegriff|Nabenlänge|Klappenöffnungswinkel|Kippvorgang|Kippwinkel|Neigungswinkel|Zulässige Personenzahl)"),
    
    # Farbe
    (300, r"^(Grundfarbe|Farbton|Farbe|Sekundär-Farbton|Korpusfarbe|Türfarbe|Farbkombination)\b|farbe$"),
    (350, r"^Front(höhe|farbe)|^Korpus"),
    
    # Ausstattung
    (400, r"^(Ausstattung|Schliessung|Schließung|Zugriff|Zubehör|Spritzschutz)"),
    (450, r"^(Rahmen|Auslieferungs|Lieferzustand)"),
    
    # Anwendung / Eignung
    (500, r"^(Geeignet\s+für|Eignung|Fasslagerung|Schildhöhe|Schildbreite|Einstellbare\s+Räumbreiten|Technische\s+Eigenschaft|Fass\s+(Durchmesser|Höhe)|Gabelzinken|Gabelrolle|Schüttkantenhöhe|Material\s+der\s+Ladefläche)"),
    
    # Sicherheit / Zulassung / Doku
    (600, r"^(DIBt-Zulassung|UN-Zulassung|Richtlinie|Hinweis)"),
    (610, r"^(Produktblätter|Sicherheitsblätter|Datenblatt)"),
    
    # Aufnahme / Versand (legacy)
    (700, r"^Aufnahme[- ]"),
    (900, r"^Versand[- ]"),
]
DEFAULT_PRIORITY = 800

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
    sonst = [r for r in rows if r['property_name'] not in HIDE_NAMES and classify(r['property_name'])==DEFAULT_PRIORITY]
    print(f"Sonstige {DEFAULT_PRIORITY}: {len(sonst)} names ({sum(r['cnt'] for r in sonst)} rows)")
    for r in sonst:
        print(f"  {r['cnt']:>5}x  {r['property_name']}")
    print()
    hide_count = frappe.db.sql("""
      SELECT COUNT(*) FROM `tabItem Ecommerce Property`
      WHERE parenttype='Item' AND sync_to_shopware=1
        AND ( LOWER(TRIM(IFNULL(property_value,''))) IN %(vals)s
              OR property_name IN %(names)s )
    """, {"vals": tuple(HIDE_VALUES), "names": tuple(HIDE_NAMES) or ("__none__",)})[0][0]
    print(f"HIDE TOTAL: {hide_count}")
    if not do_apply: 
        print("DRY RUN")
        return
    # APPLY
    frappe.db.sql("""
      UPDATE `tabItem Ecommerce Property` SET sync_to_shopware=0
      WHERE parenttype='Item' AND sync_to_shopware=1
        AND ( LOWER(TRIM(IFNULL(property_value,''))) IN %(vals)s
              OR property_name IN %(names)s )
    """, {"vals": tuple(HIDE_VALUES), "names": tuple(HIDE_NAMES) or ("__none__",)})
    frappe.db.commit()
    print(f"hidden: {hide_count}")
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
