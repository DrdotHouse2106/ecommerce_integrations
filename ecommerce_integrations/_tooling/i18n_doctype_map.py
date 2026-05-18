"""German → English map for doctype JSON ``label`` and ``description`` fields.

Applied by ``i18n_doctypes.py --apply``. Each key is the German source
string verbatim; each value is the English target. Frappe auto-marks
``label`` and ``description`` as translatable, so the German version
is shipped back via ``translations/de.csv`` and the operator-facing UI
on a German site stays unchanged.

This map intentionally covers the SSOT surface first (Product Sync,
Pull Sync, Smart Collections, Catalog Mirror, Sync Run/Error). The
large Shopware / Medusa Setting JSONs are converted in a follow-up
pass — they're operator-internal config docs where the German-first
default is less painful in the meantime.
"""

from __future__ import annotations

MAP: dict[str, str] = {
    # ── Ecommerce Product Sync ──
    "Eigene Bezeichnung für diesen Produkt-Sync, z.B. 'Hauptshop: Bestseller'.":
        "Internal label for this Product Sync, e.g. 'Main shop: best sellers'.",
    "Bei 0 läuft der Sync nur, wenn er manuell gestartet wird.":
        "When 0, the sync only runs when started manually.",
    "Priorität": "Priority",
    "Bei Konflikten (ein Artikel passt in mehrere aktive Syncs) gewinnt die höhere Priorität. Bei Gleichstand greift conflict_policy.":
        "On conflicts (an item matches multiple active syncs), higher priority wins. Ties fall back to conflict_policy.",
    "Storefronts, in denen dieses Produkt sichtbar sein soll. Mehrere möglich — das Produkt wird in jeder gelisteten Storefront ausgespielt.":
        "Storefronts where this product should be visible. Multiple allowed — the product is shipped to every listed storefront.",

    # ── Ecommerce Pull Sync ──
    "Eigene Bezeichnung für diesen Pull-Sync, z.B. 'Hauptshop Bestellungen'.":
        "Internal label for this Pull Sync, e.g. 'Main shop orders'.",
    "Bei 0 läuft der Pull nur manuell.":
        "When 0, pull only runs manually.",
    "Zeitplan": "Schedule",
    "Wie oft der Pull läuft. 'every_hour' ist Standard — Webhook + Cron als Sicherheitsnetz.":
        "How often the pull runs. 'every_hour' is the default — webhook + cron as a safety net.",
    "Was geholt wird": "What is pulled",

    # ── Ecommerce Product Sync Channel ──
    "UUID aus Shopware Setting → Verfügbare Storefronts; bei Medusa die Channel-ID.":
        "UUID from Shopware Setting → Available Storefronts; for Medusa, the channel ID.",
    "Primärer Channel": "Primary channel",
    "Markiere genau einen Channel als primär — wird für die Defaults beim Push genutzt.":
        "Mark exactly one channel as primary — used for push defaults.",
    "Wenn gesetzt, wird diese Price List für Preise in diesem Channel genutzt. Sonst die globale price_list_override des Sync-Docs.":
        "When set, this price list is used for prices in this channel. Otherwise the Sync's global price_list_override.",

    # ── Ecommerce Pull Sync Channel ──
    "UUID aus Shopware/Medusa Setting. Leer lassen für 'alle Channels'.":
        "UUID from Shopware/Medusa Setting. Leave empty for 'all channels'.",

    # ── Ecommerce Product Sync Filter Rule ──
    "Für 'in'/'not_in' kommagetrennte Werte. Für 'between' zwei Werte mit '..' getrennt (z.B. <code>100..200</code>).":
        "For 'in'/'not_in', comma-separated values. For 'between', two values separated by '..' (e.g. <code>100..200</code>).",
    "Verknüpfung mit nächster Regel": "Combinator with next rule",

    # ── Ecommerce Product Sync Override ──
    "<b>pin</b> = verknüpft den Artikel manuell mit einer Backend-Produkt-UUID. <b>skip</b> = Artikel wird in diesem Sync übersprungen. <b>custom</b> = individuelle Namens-/Preis-/Beschreibungs-Felder werden angewendet.":
        "<b>pin</b> = manually link the item to a backend product UUID. <b>skip</b> = skip the item in this sync. <b>custom</b> = apply per-item name/price/description fields.",
    "Custom Beschreibung": "Custom description",
    "Falls gesetzt: kommagetrennte sales_channel_ids, in denen dieser Artikel sichtbar sein soll (überstimmt die Sync-Default-Channels).":
        "When set: comma-separated sales_channel_ids where this item should be visible (overrides the sync's default channels).",

    # ── Ecommerce Product Sync Smart Collection (the one we just added) ──
    "Verlinkte Smart Collection — ihre Regeln entscheiden, welche Items in den Sync wandern.":
        "Linked Smart Collection — its rules decide which items enter the sync.",
    "Nur für Storefront (optional)": "Only for storefront (optional)",

    # ── Ecommerce Sync Run ──
    "Auslöser": "Trigger",
    "Zähler": "Counters",
    "Artikel in Auswahl": "Items in scope",
    "Erfolgreich": "Successful",
    "Übersprungen (Hash unverändert / Override)":
        "Skipped (hash unchanged / override)",

    # ── Ecommerce Sync Error ──
    "<b>immediate</b> = Validierungsfehler (4xx), kein automatischer Retry — Operator-Eingriff. <b>transient</b> = Timeout / 5xx — wird automatisch nochmal versucht. <b>manual_review</b> = Business-Konflikt (z.B. SKU bereits vergeben) — braucht Entscheidung.":
        "<b>immediate</b> = validation failure (4xx), no auto-retry — operator action needed. <b>transient</b> = timeout / 5xx — auto-retried. <b>manual_review</b> = business conflict (e.g. SKU taken) — needs a decision.",
    "Nächster Versuch": "Next attempt",
    "Der Sync-Run, bei dem dieser Fehler zuletzt aufgetreten ist.":
        "The Sync Run on which this error last occurred.",
    "Auflösung": "Resolution",
    "Bei Setzen wird der Eintrag aus der aktiven Retry-Queue entfernt.":
        "When set, the row is removed from the active retry queue.",

    # ── Ecommerce Catalog Mirror (a chunk of the worst offenders) ──
    "Eigene Bezeichnung für diesen Kategorie-Sync, z.B. 'Hauptshop / Produkte'. Nur zur internen Erkennung.":
        "Internal label for this category sync, e.g. 'Main shop / products'. Internal-only.",
    "Shop-Plattform, in die die Kategorien gespiegelt werden.":
        "Shop platform the categories are mirrored into.",
    "Wenn aktiviert: der zeitgesteuerte Hintergrund-Job synchronisiert diesen Kategorie-Sync regelmäßig (Cron). Wenn deaktiviert: läuft nur, wenn manuell 'Jetzt synchronisieren' geklickt wird.":
        "When enabled: the scheduled background job syncs this category sync regularly (cron). When disabled: runs only when 'Sync now' is clicked manually.",
    "Shopware-Storefront-UUID. Vorschläge werden aus 'Shopware Setting → Verfügbare Storefronts' geladen. Wenn keine Backend-Wurzel-ID gesetzt ist, wird die Navigations-Kategorie der Storefront als Wurzel verwendet.":
        "Shopware storefront UUID. Suggestions loaded from 'Shopware Setting → Available Storefronts'. If no backend-root id is set, the storefront's navigation category is used as the root.",
    "Vorschläge werden aus 'Medusa Setting → Verfügbare Storefronts' geladen. Rein informativ — Medusa hat keine Kategorisierung pro Storefront, der Wert wird vom Sync ignoriert.":
        "Suggestions loaded from 'Medusa Setting → Available Storefronts'. Informational only — Medusa has no per-storefront categorisation, the value is ignored by the sync.",
    "UUID der Backend-Kategorie für diese Artikelgruppe. Erforderlich bei Modus 'Fest verknüpfen'.":
        "UUID of the backend category for this item group. Required when mode is 'Pin'.",
    "<strong>pin</strong> = Diese Backend-ID fest verwenden (Mit bestehender Kategorie verknüpfen). <strong>skip</strong> = Diese Artikelgruppe vom Kategorie-Sync ausnehmen.":
        "<strong>pin</strong> = use this backend id explicitly (link with an existing category). <strong>skip</strong> = exclude this item group from the category sync.",

    # ── Smart Collection ──
    "Regel-Typ": "Rule type",
    "Worauf sich die Regel bezieht: <strong>Item Group</strong> (Artikelgruppe), <strong>Ecommerce Property</strong> (Eigenschaft), <strong>Manufacturer</strong> (Hersteller), <strong>Item Field</strong> (Artikel-Feld), <strong>Stock</strong> (Bestand), <strong>Brand</strong> (Marke).":
        "What the rule applies to: <strong>Item Group</strong>, <strong>Ecommerce Property</strong>, <strong>Manufacturer</strong>, <strong>Item Field</strong>, <strong>Stock</strong>, <strong>Brand</strong>.",
    "Vergleichsoperator: <strong>equals</strong> = ist genau, <strong>not_equals</strong> = ist nicht, <strong>in</strong> = ist einer aus (kommagetrennt), <strong>not_in</strong> = ist keiner aus, <strong>descends_from</strong> = ist untergeordnet von (für Artikelgruppen-Baum), <strong>contains</strong> = enthält, <strong>regex</strong> = passt auf regulären Ausdruck, <strong>is_set</strong> = ist gesetzt, <strong>is_empty</strong> = ist leer.":
        "Comparison operator: <strong>equals</strong>, <strong>not_equals</strong>, <strong>in</strong> = is one of (comma-separated), <strong>not_in</strong>, <strong>descends_from</strong> = is descended from (for the item-group tree), <strong>contains</strong>, <strong>regex</strong>, <strong>is_set</strong>, <strong>is_empty</strong>.",
    "Einzelner Wert, oder kommagetrennt für 'in' / 'not_in'. Wird bei 'is_set' / 'is_empty' ignoriert.":
        "Single value, or comma-separated for 'in' / 'not_in'. Ignored for 'is_set' / 'is_empty'.",
    "Regeln mit derselben OR-Gruppen-Nummer (außer 0) werden mit OR verknüpft. Unterschiedliche OR-Gruppen werden über die Regel-Verknüpfung der Eigenen Kollektion kombiniert.":
        "Rules with the same OR-group number (except 0) are combined with OR. Different OR-groups are combined via the Smart Collection's rule combinator.",
    "Storefront-ID oder -Name im Zielsystem. Wird beim Sync gegen die Storefront-Tabelle des gewählten Backends aufgelöst.":
        "Storefront ID or name in the target system. Resolved against the chosen backend's storefront table at sync time.",
    "Wenn deaktiviert, wird dieses Sync-Ziel übersprungen.":
        "When disabled, this sync target is skipped.",
    "Shopware-Schalter. <strong>30 - All</strong> = überall sichtbar. <strong>20 - Linked</strong> = nur über Direktlink. <strong>10 - Search</strong> = nur in der Suche. <strong>0 - Hidden</strong> = nicht sichtbar. Medusa behandelt alles außer 'Hidden' als sichtbar.":
        "Shopware switch. <strong>30 - All</strong> = visible everywhere. <strong>20 - Linked</strong> = direct link only. <strong>10 - Search</strong> = search only. <strong>0 - Hidden</strong> = not visible. Medusa treats everything except 'Hidden' as visible.",
    "Nur hinzufügen, nichts entfernen": "Add only, never remove",
    "Wenn aktiviert: Der Sync fügt nur Artikel zur Backend-Kategorie hinzu. Artikel, die bereits dort sind aber nicht ins Regelergebnis passen, bleiben unangetastet. Sinnvoll beim Verknüpfen mit einer manuell gepflegten Kategorie.":
        "When enabled: the sync only adds items to the backend category. Items already there but not matching the rule result are left alone. Useful when linking to a manually curated category.",

    # ── Smart Collection (main) ──
    "Interner Bezeichner (Frappe-Dokumentname). Wird mit dem Storefront-Suffix eindeutig gemacht. Nachträglich änderbar.":
        "Internal identifier (Frappe document name). Made unique with the storefront suffix. Editable later.",
    "Optional: Handle/SEO-URL im Shop (z.B. Medusa-Category-Handle, Shopware-seoUrl). Wenn leer, wird der Kurzname verwendet. Setzen Sie diesen Wert, wenn Sie im Shop eine kürzere URL möchten.":
        "Optional: handle / SEO URL in the shop (e.g. Medusa category handle, Shopware seoUrl). When empty, the slug is used. Set this for a shorter URL in the shop.",
    "<strong>Aktiv</strong>: Die Kollektion wird automatisch durch den zeitgesteuerten Sync (Cron) aktualisiert, sobald sich Regelergebnisse ändern. <strong>Inaktiv</strong>: Die Kollektion wird nur manuell über die Schaltfläche 'Jetzt synchronisieren' gepflegt.":
        "<strong>Active</strong>: the collection is updated automatically by the scheduled sync (cron) whenever rule results change. <strong>Inactive</strong>: the collection is maintained only via the 'Sync now' button.",
    "Übergeordnete Kollektion (optional)": "Parent collection (optional)",
    "Optional: Eltern-Kollektion für verschachtelte Kategorien (nur bei Backends, die das unterstützen).":
        "Optional: parent collection for nested categories (only on backends that support it).",

    # ── Channel Override / Branding (smaller chunks) ──
    "Shop-Plattform, für die diese Sichtbarkeit gilt.":
        "Shop platform this visibility rule applies to.",
    "<strong>Einschließen</strong> = Artikel zusätzlich in dieser Storefront sichtbar machen. <strong>Ausschließen</strong> = Artikel aus der Storefront ausschließen, auch wenn andere Regeln ihn einschließen würden.":
        "<strong>Include</strong> = make the item visible in this storefront. <strong>Exclude</strong> = exclude the item from the storefront, even if other rules would include it.",
    "Nur relevant bei Modus 'Einschließen'. <strong>30 - All</strong> = überall sichtbar (Kategorien, Suche, Direktlink). <strong>20 - Linked</strong> = nur über Direktlink erreichbar. <strong>10 - Search</strong> = nur in der Suche auffindbar.":
        "Only relevant for 'Include' mode. <strong>30 - All</strong> = visible everywhere (categories, search, direct link). <strong>20 - Linked</strong> = direct link only. <strong>10 - Search</strong> = search only.",

    # ── Sales-channel sandbox flag (Shopware + Medusa) ──
    "Markiert diesen Channel als Sandbox. Nur so markierte Channels erscheinen im Sandbox-Push-Dialog der Product Syncs ohne Production-Warnung.":
        "Marks this channel as sandbox. Only sandbox-flagged channels appear in the Product Sync sandbox-push dialog without a production warning.",
}
