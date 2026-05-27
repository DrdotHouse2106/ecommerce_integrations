def main():
    import frappe, re
    CATS = [
        (100, r"^(Breite|Länge|Höhe|Tiefe|Gewicht|Maße)(\s*\(mm\))?$"),
        (110, r"^Gesamt(breite|länge|höhe|tiefe)"),
        (120, r"^(Lichte|Innen|Außen)\s+(Breite|Länge|Höhe|Tiefe)"),
        (150, r"bereich$"),
        (200, r"^(Material|Oberfläche|Bauweise|Konstruktion)$"),
        (220, r"^Regal(system|tiefe|art|typ)?$"),
        (250, r"^(Tragkraft|Feldlast|Belastung|Anzahl)\s|^Anzahl\s|^Tragkraft\s"),
        (260, r"^Schubladen?\s"),
        (270, r"^Verstellraster$|^Auflage"),
        (300, r"^(Grundfarbe|Farbton|Farbe)\b|farbe$"),
        (350, r"^Front(höhe|farbe)|^Korpus"),
        (400, r"^(Ausstattung|Schliessung|Zugriff)"),
        (450, r"^Rahmen\s|^Auslieferungs|^Lieferzustand"),
        (700, r"^Aufnahme[- ]"),
        (900, r"^Versand[- ]"),
    ]
    def classify(name):
        for prio, p in CATS:
            if re.search(p, name, re.IGNORECASE):
                return prio
        return 500
    rows = frappe.db.sql("""
      SELECT property_name, COUNT(*) cnt FROM `tabItem Ecommerce Property`
      WHERE parenttype='Item' AND sync_to_shopware=1
      GROUP BY property_name
      ORDER BY cnt DESC
    """, as_dict=True)
    sonstige = [r for r in rows if classify(r['property_name'])==500]
    print(f"=== 206 Sonstige property names (alphabetisch, cnt desc) ===\n")
    for r in sonstige:
        print(f"  {r['cnt']:>6}x  {r['property_name']}")
