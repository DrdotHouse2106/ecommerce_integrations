"""German → English translation map for the source-code conversion pass.

The keys are the strings as they currently sit inside ``_('...')`` /
``__('...')`` calls in the source files. The values are the English
equivalents we want to replace them with. Once a key is moved here,
the apply script (``i18n_apply.py``) rewrites every source-file
occurrence in place and adds an English-source → German-target row
to ``translations/de.csv`` so German operators still see the original
text.

Editing rules:

- Keep ``{0}``/``{1}``/``{n}`` placeholders byte-for-byte.
- Preserve trailing whitespace, ellipses, leading symbols.
- Translation is normal-tone English: title-case for short labels,
  sentence-case for sentences.
"""

from __future__ import annotations

MAP: dict[str, str] = {
    # ── Operator-facing labels (short) ──
    "Apply läuft…": "Apply running…",
    "Auflösung": "Resolution",
    "Auffällige Änderungen": "Significant changes",
    "Auswahl-Modus": "Scope mode",
    "Auswahl: Behalten": "Keep selection",
    "Auswahl: Löschen": "Delete selection",
    "Beschreibung": "Description",
    "Bereit für Vorschau": "Ready for preview",
    "Keine Änd.": "No change",
    "Keine Änderung": "No change",
    "Neu laden": "Reload",
    "Schließen": "Close",
    "Schnellvorschau öffnen": "Open quick preview",
    "Setup": "Setup",
    "Späterer Zeitpunkt": "Later",
    "Später": "Later",
    "Verbindung": "Connection",
    "Vorschau": "Preview",
    "Vorschau öffnen": "Open preview",
    "Vorschau fehlgeschlagen": "Preview failed",
    "Vorschau konnte nicht gestartet werden.": "Could not start preview.",
    "Vorschau: {0}": "Preview: {0}",
    "Warnung": "Warning",
    "Wurzel aufgelöst": "Root resolved",
    "Wurzel nicht aufgelöst": "Root not resolved",
    "noch nicht aufgelöst": "not yet resolved",
    "Outbound": "Outbound",
    "Inbound": "Inbound",
    "Advanced": "Advanced",
    "Artikel in Auswahl": "Items in scope",
    "Pre-Flight läuft…": "Pre-flight running…",
    "SKU-Matching läuft…": "SKU matching running…",
    "Zum Löschen markiert": "Marked for deletion",
    "Verknüpfung verschoben": "Mapping moved",
    "Verlorene Verknüpfungen": "Lost mappings",
    "Mit Artikelgruppe verknüpfen": "Link with item group",
    "Mehrdeutig (übersprungen)": "Ambiguous (skipped)",
    "Schon zugeordnet (übersprungen)": "Already mapped (skipped)",
    "0 ausgewählt": "0 selected",
    "{0} ausgewählt": "{0} selected",

    # ── Dialog titles + buttons ──
    "Catalog Mirror öffnen": "Open Catalog Mirror",
    "Pull Sync öffnen": "Open Pull Sync",
    "Product Sync öffnen": "Open Product Sync",
    "Nächster Schritt: Product Sync anlegen": "Next step: create a Product Sync",
    "Vollständig (inkl. Orphans)…": "Full (incl. orphans)…",
    "Backend-Kategorie löschen": "Delete backend category",
    "Zuordnung löschen & neu anlegen": "Clear mapping and re-create",
    "Vorhandene Backend-Kategorien per Pfad-Match zuordnen? ":
        "Match existing backend categories by path? ",

    # ── Sentences / help text ──
    "(Wert nur über Form-Feld sichtbar)": "(value only visible via form field)",
    "Auswahl ist leer — kein Artikel passt zum Scope.":
        "Scope is empty — no item matches it.",
    "Auswahl enthält {0}+ Artikel.": "Scope contains {0}+ items.",
    "Auswahl wird ermittelt…": "Resolving scope…",
    "Backend-Abgleich läuft (Orphan- und Drift-Erkennung — kann bei großen Katalogen mehrere Minuten dauern)…":
        "Backend reconciliation running (orphan and drift detection — may take several minutes on large catalogues)…",
    "Backend-Kategorien ohne ERPNext-Pendant":
        "Backend categories without ERPNext counterpart",
    "Artikelgruppen, deren gespeicherte Backend-Kategorie nicht mehr im Live-Baum existiert.":
        "Item groups whose stored backend category no longer exists in the live tree.",
    "Damit Produkte aus ERPNext nach {0} fliessen, brauchst du einen <b>Product Sync</b>. Dort wählst du Auswahl (Kategorie, Smart Collection, …), Storefronts und Sync-Zeitplan.":
        "For products to flow from ERPNext to {0} you need a <b>Product Sync</b>. There you pick scope (category, smart collection, …), storefronts, and sync schedule.",
    "Dieser Push verändert echte Daten in {0}. Sicher fortfahren?":
        "This push changes live data in {0}. Continue?",
    "Empfang aus Medusa: Bestellungen, Kunden, Bestand kommen rein in ERPNext (sicher empfohlen für den Einstieg).":
        "Receive from Medusa: orders, customers, stock flow into ERPNext (safe default for getting started).",
    "Empfang aus Shopware: Bestellungen, Kunden, Bestand kommen rein in ERPNext (sicher empfohlen für den Einstieg).":
        "Receive from Shopware: orders, customers, stock flow into ERPNext (safe default for getting started).",
    "Es gibt {0} geplante Neuanlagen und {1} verwaiste Live-Kategorien. ":
        "{0} planned creates and {1} orphan live categories. ",
    "Keine Artikel in der Auswahl gefunden.": "No items found in scope.",
    "Keine Ziel-Storefronts konfiguriert — Apply würde nichts pushen.":
        "No target storefronts configured — Apply would push nothing.",
    "Lass aus, bis ein Pre-Flight + Dry-Run grün sind.":
        "Leave off until pre-flight + dry-run are both green.",
    "Mirror ist deaktiviert — dies ist nur eine Vorschau. ":
        "Mirror is disabled — this is preview-only. ",
    "Noch keine Test-Läufe.": "No test runs yet.",
    "Orphan-Erkennung übersprungen: {0}": "Orphan detection skipped: {0}",
    "Scope-Auflösung fehlgeschlagen: {0}": "Scope resolution failed: {0}",
    "Vollautomatisch (Fortgeschritten): Item-Saves pushen sofort nach Medusa, Catalog Mirror Cron läuft stündlich (mit Bedacht aktivieren).":
        "Fully automatic (advanced): item saves push to Medusa immediately, Catalog Mirror cron runs hourly (enable with care).",
    "Vollautomatisch (Fortgeschritten): Item-Saves pushen sofort nach Shopware, Catalog Mirror Cron läuft stündlich (mit Bedacht aktivieren).":
        "Fully automatic (advanced): item saves push to Shopware immediately, Catalog Mirror cron runs hourly (enable with care).",
    "Vorschau-JSON konnte nicht gelesen werden.": "Could not read preview JSON.",
    "Welche Artikel der Sync abdeckt. Verfeinerung über das Formular.":
        "Which items the sync covers. Refine via the form.",
    "Wirklich apply? Die Aktion kann nur per Rollback rückgängig gemacht werden.":
        "Really apply? The action can only be undone via rollback.",
    "Öffne den Setup Wizard, um automatisch ein sicheres Secret zu erzeugen, oder trage manuell ein Secret im Webhook-Bereich ein.":
        "Open the Setup Wizard to auto-generate a secure secret, or paste one into the webhook section manually.",
    "Übersprungen: {0}": "Skipped: {0}",
    "Wert nur über Form-Feld sichtbar": "Value only visible via form field",
    "zeigen auf gelöschte Backend-Kategorien. Siehe Abschnitt unten.":
        "point at deleted backend categories. See section below.",
    "{0} Backend-Kategorien löschen? Diese Aktion kann nicht rückgängig gemacht werden.":
        "Delete {0} backend categories? This action cannot be undone.",
    "{0} Kategorien: {1}": "{0} categories: {1}",

    # ── Round-2 pickups ──
    "Sync ist nicht aktiv — Cron würde ihn überspringen.":
        "Sync is inactive — cron would skip it.",
    "Sync läuft": "Sync running",
    "Sync öffnen": "Open sync",
    "Sync-Fehler": "Sync error",
    "Sync-Fehler: {0}": "Sync error: {0}",
    "Teilweise erfolgreich": "Partial success",
    "UUID aus dem Setting → Verfügbare Storefronts. Eine, weitere via Formular.":
        "UUID from Setting → Available Storefronts. One here, more via the form.",
    "Unbekannter Fehler.": "Unknown error.",
    "Unerwarteter Fehler beim Adapter-Load: {0}":
        "Unexpected error loading adapter: {0}",
    "Verknüpfte Kollektion": "Linked collection",
    "Verknüpfter Kategorie-Sync": "Linked category sync",

    # ── Category-bridge finding (from preflight_check) ──
    "{0} Artikelgruppen haben noch keine Backend-Kategorie. Produkte würden ohne Kategorie-Zuordnung gepushed.":
        "{0} item groups have no backend category yet. Products would be pushed without a category link.",
    "Catalog Mirror öffnen → Apply Live ausführen, bevor Product Sync läuft.":
        "Open Catalog Mirror → run Apply Live before Product Sync.",
}
