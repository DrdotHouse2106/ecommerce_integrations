"""Shared classifier for ``Item Ecommerce Property`` names.

Both the apply pipeline (when upserting Shopware ``property_group``
rows) and any one-off ERPNext-side cleanup script rely on the same
priority table — extracting it into a single module keeps the
ordering consistent between "what we tell Shopware to display
where" and "how the operator sees properties grouped in ERPNext".

Lower priority numbers appear first on the storefront PDP. The
buckets are deliberately coarse — Shopware does not support
per-product property ordering, so the priority maps to
``property_group.position`` which is a single global field per
group.
"""

from __future__ import annotations

import re

# (priority, regex pattern) — first match wins, regex is case-insensitive.
_CATEGORIES: tuple[tuple[int, str], ...] = (
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
)
_DEFAULT_PRIORITY = 800  # "Sonstige"

# Pre-compile patterns once.
_COMPILED: tuple[tuple[int, "re.Pattern[str]"], ...] = tuple(
    (prio, re.compile(pattern, re.IGNORECASE))
    for prio, pattern in _CATEGORIES
)


def property_priority(name: str) -> int:
    """Return the Shopware ``property_group.position`` to apply for a
    property of this name. First matching regex wins; unmatched
    names land at ``_DEFAULT_PRIORITY`` so they appear after every
    classified category but before any explicitly-suppressed one."""
    for prio, compiled in _COMPILED:
        if compiled.search(name):
            return prio
    return _DEFAULT_PRIORITY
