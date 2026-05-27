def main():
    import frappe, re
    CATEGORIES = [
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
        (200, r"^(Material|Oberfläche|Bauweise|Konstruktion|Bodentyp|Bodenart|Türmaterial|Plattenstärke|Arbeitsplatte|Aussteifungsart|Rost|RAL-Nummer|Material\s+(der|Arbeitsplatte))"),
        (210, r"^(Aufstellung|Montageart|Türausführung|Spritzschutzwand|PE-Einsatz)$"),
        (220, r"^(Regal(system|tiefe|art|typ)?|System|Typ|Ausführung|Modul|Grund-/Anbauregal|Bauweise|Felge|Bereifung|Radlagerung)"),
        (250, r"^(Tragkraft|Tragfähigkeit|Feldlast|Belastung|Belastbarkeit|Belastb|Fachlast|Auflast|Radlast|Flächenlast|Stützlast|Zuggewicht|Zulässiges|Förderleistung|Streubreite|Maximale\s+(Streubreite|Dichte|Stützlast)|Max\.\s+\w|Stapelbar)"),
        (256, r"Anzahl|^Maximale?\s+Anzahl"),
        (260, r"^Schubladen?\s|^Anzahl Schubladen"),
        (270, r"^Verstellraster$|^Auflage|^Verstellbar|^Höhenverstellbar"),
        (275, r"^(Inhalt|Auffangvolumen|Volumen|Mindestölbedarf)"),
        (280, r"^(Bereifung|Radgröße|Radtyp|Radlagerung|Felge|Fahrbar|Rangierhilfe|Schiebegriff|Nabenlänge|Klappenöffnungswinkel|Kippvorgang|Kippwinkel|Neigungswinkel|Zulässige Personenzahl)"),
        (300, r"^(Grundfarbe|Farbton|Farbe|Sekundär-Farbton|Korpusfarbe|Türfarbe|Farbkombination)\b|farbe$"),
        (350, r"^Front(höhe|farbe)|^Korpus"),
        (400, r"^(Ausstattung|Schliessung|Schließung|Zugriff|Zubehör|Spritzschutz)"),
        (450, r"^(Rahmen|Auslieferungs|Lieferzustand)"),
        (500, r"^(Geeignet\s+für|Eignung|Fasslagerung|Schildhöhe|Schildbreite|Einstellbare\s+Räumbreiten|Technische\s+Eigenschaft|Fass\s+(Durchmesser|Höhe)|Gabelzinken|Gabelrolle|Schüttkantenhöhe|Material\s+der\s+Ladefläche)"),
        (600, r"^(DIBt-Zulassung|UN-Zulassung|Richtlinie|Hinweis)"),
        (610, r"^(Produktblätter|Sicherheitsblätter|Datenblatt)"),
        (700, r"^Aufnahme[- ]"),
        (900, r"^Versand[- ]"),
    ]
    def classify(name):
        for prio, p in CATEGORIES:
            if re.search(p, name, re.IGNORECASE):
                return prio
        return 800
    parents = frappe.db.sql("SELECT DISTINCT parent FROM `tabItem Ecommerce Property` WHERE parenttype='Item'", pluck="parent")
    print(f"reordering {len(parents)} parents in chunks of 200...")
    CHUNK = 200
    for i in range(0, len(parents), CHUNK):
        for parent in parents[i:i+CHUNK]:
            rs = frappe.db.sql("SELECT name, property_name FROM `tabItem Ecommerce Property` WHERE parent=%s AND parenttype='Item'", (parent,), as_dict=True)
            rs.sort(key=lambda r: (classify(r['property_name']), r['property_name']))
            for new_idx, r in enumerate(rs, start=1):
                frappe.db.set_value("Item Ecommerce Property", r['name'], "idx", new_idx, update_modified=False)
        frappe.db.commit()
        if (i // CHUNK) % 10 == 0:
            print(f"  {i+CHUNK}/{len(parents)} parents committed")
    print("DONE.")
