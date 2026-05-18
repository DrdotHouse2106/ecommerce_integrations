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

    # ─────────────────────────────────────────────────────────────────────
    # Round 4 — Settings JSONs (Shopware Setting + Medusa Setting +
    # remaining bits across Catalog Mirror, Smart Collection, Pull Sync,
    # Sync Run/Error, Ecommerce Product Sync, Channel Branding).
    # ─────────────────────────────────────────────────────────────────────

    # ── Activation toggles + headers ──
    "Shopware-6-Integration aktivieren": "Enable Shopware 6 integration",
    "Medusa-Integration aktivieren": "Enable Medusa integration",
    "Aktiviert die Verbindung zu Shopware. Deaktiviert: keine Synchronisation, keine Webhook-Verarbeitung. Erst aktivieren, wenn die Verbindung getestet wurde.":
        "Enables the connection to Shopware. Disabled: no sync, no webhook processing. Only enable once the connection has been tested.",
    "Aktiviert die Verbindung zu Medusa. Deaktiviert: keine Synchronisation, keine Webhook-Verarbeitung. Erst aktivieren, wenn die Verbindung getestet wurde.":
        "Enables the connection to Medusa. Disabled: no sync, no webhook processing. Only enable once the connection has been tested.",

    # ── Connection / credentials ──
    "Vollständige Adresse Ihres Shopware-6-Shops, z.B. https://yourshop.example.com (mit https://, ohne abschließenden Schrägstrich).":
        "Full URL of your Shopware 6 shop, e.g. https://yourshop.example.com (with https://, no trailing slash).",
    "Vollständige Adresse Ihres Medusa-Backends, z.B. https://medusa.example.com (mit https://, ohne abschließenden Schrägstrich).":
        "Full URL of your Medusa backend, e.g. https://medusa.example.com (with https://, no trailing slash).",
    "Zugangsdaten zur Shopware-Admin-API. Nach dem Eintragen über die Schaltfläche oben rechts die Verbindung testen.":
        "Shopware Admin API credentials. After entering, test the connection via the button at the top right.",
    "Zugangsdaten zur Medusa-Admin-API. Nach dem Eintragen über die Schaltfläche oben rechts die Verbindung testen.":
        "Medusa Admin API credentials. After entering, test the connection via the button at the top right.",
    "Admin-API-Schlüssel aus Medusa (Einstellungen → API-Schlüssel-Verwaltung). Wird verschlüsselt gespeichert.":
        "Medusa Admin API key (Settings → API Key Management). Stored encrypted.",
    "API-Schlüssel": "API key",
    "Admin-Benutzername für den API-Zugriff.": "Admin username for API access.",
    "Admin-Passwort für den API-Zugriff. Wird verschlüsselt gespeichert.":
        "Admin password for API access. Stored encrypted.",
    "In Shopware unter Einstellungen → System → Integrationen eine neue Integration anlegen und Client-ID + Secret hier eintragen.":
        "In Shopware: Settings → System → Integrations, create a new integration and paste Client ID + Secret here.",
    "Integration-Zugangsdaten (Shopware Admin > Einstellungen > System > Integrationen)":
        "Integration credentials (Shopware Admin > Settings > System > Integrations)",
    "Benutzer-Zugangsdaten (Shopware Admin > Einstellungen > System > Benutzer)":
        "User credentials (Shopware Admin > Settings > System > Users)",
    "Zugriffsschlüssel-ID (Access Key ID) der Shopware-Integration.":
        "Access Key ID of the Shopware integration.",
    "Geheimer Zugriffsschlüssel (Secret Access Key) der Shopware-Integration. Wird verschlüsselt gespeichert.":
        "Secret Access Key of the Shopware integration. Stored encrypted.",
    "Übertragungsweg": "Transport",

    # ── Storefronts ──
    "Storefronts (Verkaufskanäle)": "Storefronts (sales channels)",
    "Verfügbare Storefronts": "Available storefronts",
    "Aus Shopware abgerufene Storefronts. Über die Schaltfläche unten neu laden.":
        "Storefronts fetched from Shopware. Reload via the button below.",
    "Aus Medusa abgerufene Storefronts. Pro Eintrag aktivieren/deaktivieren, ob Artikel dort verfügbar sein sollen.":
        "Storefronts fetched from Medusa. Toggle each row to control whether items are available there.",

    # ── Webhook ──
    "Webhook-Geheimcode": "Webhook secret",
    "Webhooks (Eingehende Ereignisse)": "Webhooks (incoming events)",
    "Webhooks erlauben Shopware, ERPNext über Ereignisse (z.B. neue Bestellung) sofort zu informieren — schneller als der reguläre Abruf.":
        "Webhooks let Shopware notify ERPNext of events (e.g. new order) instantly — faster than periodic polling.",
    "Webhooks erlauben Medusa, ERPNext über Ereignisse (z.B. neue Bestellung) sofort zu informieren — schneller als der reguläre Abruf. Erfordert das Medusa-Subscriber-Plugin.":
        "Webhooks let Medusa notify ERPNext of events (e.g. new order) instantly — faster than periodic polling. Requires the Medusa subscriber plugin.",
    "Geheimcode zur Prüfung eingehender Webhooks. Im produktiven Einsatz ZWINGEND erforderlich. Den gleichen Wert auch in Shopware bei der Webhook-Konfiguration eintragen.":
        "Secret used to verify incoming webhooks. MANDATORY in production. Configure the same value in Shopware's webhook setup.",
    "Geheimcode zur Prüfung eingehender Medusa-Webhooks. Im produktiven Einsatz ZWINGEND erforderlich. Den gleichen Wert auch im Medusa-Subscriber konfigurieren.":
        "Secret used to verify incoming Medusa webhooks. MANDATORY in production. Configure the same value in the Medusa subscriber.",
    "SICHERHEITSWARNUNG: Nur für lokale Tests aktivieren. Im Live-Betrieb IMMER deaktiviert lassen und stattdessen einen Webhook-Geheimcode setzen — sonst kann jede Person mit der URL Daten einschleusen.":
        "SECURITY WARNING: Only enable for local testing. ALWAYS leave disabled in production and set a webhook secret instead — otherwise anyone with the URL can inject data.",

    # ── Outbound (product upload) ──
    "ERPNext-Artikel zu Shopware hochladen": "Upload ERPNext items to Shopware",
    "ERPNext-Artikel zu Medusa hochladen": "Upload ERPNext items to Medusa",
    "Ob und wie Artikel von ERPNext zu Shopware übertragen werden. ACHTUNG: Mit diesen Einstellungen können bestehende Shopware-Daten überschrieben werden. Vor dem Aktivieren immer einen Trockenlauf durchführen.":
        "Whether and how items are pushed from ERPNext to Shopware. CAUTION: These settings can overwrite existing Shopware data. Always run a dry-run before enabling.",
    "Ob und wie Artikel von ERPNext zu Medusa übertragen werden. ACHTUNG: Mit diesen Einstellungen können bestehende Medusa-Daten überschrieben werden. Vor dem Aktivieren immer einen Trockenlauf durchführen.":
        "Whether and how items are pushed from ERPNext to Medusa. CAUTION: These settings can overwrite existing Medusa data. Always run a dry-run before enabling.",
    "Artikel-Änderungen automatisch nach Shopware übertragen":
        "Automatically push item changes to Shopware",
    "Artikel-Änderungen automatisch nach Medusa übertragen":
        "Automatically push item changes to Medusa",
    "GEFÄHRLICH: Aktiviert den Outbound-Sync. Jeder neue Artikel und jede Artikel-Änderung wird zu Shopware übertragen. WARNUNG: Wenn Sie Produkttexte direkt in Shopware pflegen, werden diese beim nächsten ERPNext-Speichern überschrieben. Bitte zuerst einen Komplett-Sync als Trockenlauf ausführen, um die Auswirkungen zu sehen.":
        "DANGEROUS: Enables outbound sync. Every new item and every item change is pushed to Shopware. WARNING: If you edit product copy directly in Shopware, it will be overwritten on the next ERPNext save. Run a full dry-run first to see the impact.",
    "GEFÄHRLICH: Aktiviert den Outbound-Sync. Jeder neue Artikel und jede Artikel-Änderung wird zu Medusa übertragen. WARNUNG: Wenn Sie Produkttexte direkt in Medusa pflegen, werden diese beim nächsten ERPNext-Speichern überschrieben. Bitte zuerst einen Komplett-Sync als Trockenlauf ausführen, um die Auswirkungen zu sehen.":
        "DANGEROUS: Enables outbound sync. Every new item and every item change is pushed to Medusa. WARNING: If you edit product copy directly in Medusa, it will be overwritten on the next ERPNext save. Run a full dry-run first to see the impact.",
    "Wenn aktiviert: jede Artikel-Änderung wird sofort zu Shopware gesendet. Wenn deaktiviert: Synchronisation nur manuell pro Artikel über die Schaltfläche 'Jetzt synchronisieren'. Wer Texte oft in Shopware nachpflegt, sollte diesen Schalter ausschalten.":
        "When enabled: every item change is pushed to Shopware immediately. When disabled: sync only happens manually per item via the 'Sync now' button. Operators who often edit copy in Shopware should leave this off.",
    "Wenn aktiviert: jede Artikel-Änderung wird sofort zu Medusa gesendet. Wenn deaktiviert: Synchronisation nur manuell pro Artikel über 'Jetzt synchronisieren'.":
        "When enabled: every item change is pushed to Medusa immediately. When disabled: sync only happens manually per item via 'Sync now'.",
    "Neue Artikel direkt aktiv in Shopware veröffentlichen":
        "Publish new items as active in Shopware",
    "Neue Artikel direkt aktiv in Medusa veröffentlichen":
        "Publish new items as active in Medusa",
    "Wenn aktiviert: neu hochgeladene Artikel sind sofort im Shop sichtbar. Wenn deaktiviert (empfohlen): Artikel werden als inaktiv angelegt und müssen in Shopware manuell freigegeben werden.":
        "When enabled: newly uploaded items are visible in the shop immediately. When disabled (recommended): items are created as inactive and have to be released manually in Shopware.",
    "Wenn aktiviert: neu hochgeladene Artikel sind sofort im Shop sichtbar. Wenn deaktiviert: Artikel werden als inaktiv angelegt und müssen in Medusa manuell freigegeben werden.":
        "When enabled: newly uploaded items are visible in the shop immediately. When disabled: items are created as inactive and have to be released manually in Medusa.",

    # ── Bulk sync ──
    "Massen-Synchronisation aktivieren": "Enable bulk synchronisation",
    "Empfohlen: aktiviert. Wenn viele Artikel kurz hintereinander geändert werden, werden die Änderungen in einer Warteschlange gesammelt und stapelweise an Shopware übertragen. Verhindert Lastspitzen und Timeouts.":
        "Recommended: enabled. When many items change in quick succession, the changes are queued and pushed to Shopware in batches. Prevents load spikes and timeouts.",
    "Schützt ERPNext vor Überlastung bei vielen Artikel-Änderungen in kurzer Zeit. Schnelle Änderungen werden gesammelt und in Stapeln verarbeitet.":
        "Shields ERPNext from overload when many item changes happen in a short time. Fast changes are collected and processed in batches.",
    "Ab wie vielen Artikel-Änderungen innerhalb von 2 Sekunden in den Massen-Modus gewechselt wird.":
        "Threshold of item changes within 2 seconds at which the bulk mode kicks in.",

    # ── Inventory ──
    "ERPNext-Lagerbestände zu Shopware synchronisieren":
        "Sync ERPNext stock levels to Shopware",
    "Lagerbestände zu Medusa synchronisieren":
        "Sync stock levels to Medusa",
    "GEFÄHRLICH: Wenn aktiviert, überschreibt ERPNext die Bestände in Shopware periodisch. Nur aktivieren, wenn ERPNext das führende System für Bestände ist. Falls Sie Bestand auch direkt in Shopware pflegen, würde diese Pflege überschrieben.":
        "DANGEROUS: When enabled, ERPNext overwrites stock in Shopware periodically. Only enable if ERPNext is the source of truth for stock. If you also maintain stock in Shopware, those edits would be overwritten.",
    "GEFÄHRLICH: Wenn aktiviert, überschreibt ERPNext die Bestände in Medusa periodisch. Nur aktivieren, wenn ERPNext das führende System für Bestände ist.":
        "DANGEROUS: When enabled, ERPNext overwrites stock in Medusa periodically. Only enable if ERPNext is the source of truth for stock.",
    "Wie ERPNext-Lagerbestände zu Shopware übertragen werden. Bei mehreren Lagern bitte die Zuordnungstabelle unten ausfüllen.":
        "How ERPNext stock levels are pushed to Shopware. For multi-warehouse setups, fill out the mapping table below.",
    "Wie ERPNext-Lagerbestände zu Medusa übertragen werden. Bei mehreren Lagern bitte die Lager-Zuordnungstabelle unten ausfüllen.":
        "How ERPNext stock levels are pushed to Medusa. For multi-warehouse setups, fill out the warehouse mapping table below.",
    "Wie oft ERPNext-Bestände zu Shopware übertragen werden.":
        "How often ERPNext stock is pushed to Shopware.",
    "Wie oft ERPNext-Bestände zu Medusa übertragen werden.":
        "How often ERPNext stock is pushed to Medusa.",
    "Standard-Lager, aus dem Bestände gemeldet werden, wenn keine spezifische Lager-Zuordnung greift.":
        "Default warehouse used to report stock when no specific warehouse mapping matches.",
    "ID des Medusa-Lagerorts (Stock Location), an den Bestände gemeldet werden. Bei mehreren Lagern bitte die Zuordnungstabelle unten ausfüllen.":
        "ID of the Medusa Stock Location to which stock is reported. For multi-warehouse setups, fill out the mapping table below.",
    "ERPNext-Lager auf Medusa-Lagerorte abbilden für Multi-Lager-Bestand.":
        "Map ERPNext warehouses to Medusa stock locations for multi-warehouse stock.",
    "Standard-Lager für eingehende Bestellungen.":
        "Default warehouse for incoming orders.",

    # ── Pricing ──
    "Welche Preisliste an Shopware übertragen wird, und wie mit Brutto-/Netto-Preisen umgegangen wird. Wichtig: B2B-Shops arbeiten typischerweise mit Netto-Preisen, B2C-Shops mit Brutto-Preisen.":
        "Which price list is pushed to Shopware and how gross/net prices are handled. Note: B2B shops typically use net prices, B2C shops gross prices.",
    "Welche Preisliste an Medusa übertragen wird, und wie mit Brutto-/Netto-Preisen umgegangen wird. Wichtig: B2B-Shops arbeiten typischerweise mit Netto-Preisen, B2C-Shops mit Brutto-Preisen.":
        "Which price list is pushed to Medusa and how gross/net prices are handled. Note: B2B shops typically use net prices, B2C shops gross prices.",
    "Standard-Preisliste enthält Brutto-Preise":
        "Default price list contains gross prices",
    "Preisliste enthält Brutto-Preise":
        "Price list contains gross prices",
    "UVP-Preisliste enthält Brutto-Preise":
        "MSRP price list contains gross prices",
    "Aktivieren, wenn die Standard-Preisliste Brutto-Preise (inkl. MwSt.) enthält. Die Preise werden vor der Synchronisation zu Shopware in Netto umgerechnet.":
        "Enable if the default price list contains gross prices (incl. VAT). Prices are converted to net before syncing to Shopware.",
    "Aktivieren, wenn die Preislisten Brutto-Preise (inkl. MwSt.) enthalten. Die Preise werden vor der Übertragung zu Medusa in Netto umgerechnet.":
        "Enable if the price lists contain gross prices (incl. VAT). Prices are converted to net before sync to Medusa.",
    "Aktivieren, wenn die UVP-Preisliste Brutto-Preise enthält. Die Preise werden vor dem Senden an Shopware in Netto umgerechnet.":
        "Enable if the MSRP price list contains gross prices. Prices are converted to net before being sent to Shopware.",
    "Empfohlen für B2B: Preise aus Shopware werden als Netto-Preise gespeichert. Wenn deaktiviert (B2C), bleiben sie als Brutto-Preise (inkl. MwSt.) erhalten.":
        "Recommended for B2B: prices from Shopware are stored as net prices. When disabled (B2C), they remain as gross prices (incl. VAT).",
    "Empfohlen für B2B: Preise aus Medusa werden als Netto-Preise gespeichert. Wenn deaktiviert (B2C), bleiben sie als Brutto-Preise (inkl. MwSt.) erhalten.":
        "Recommended for B2B: prices from Medusa are stored as net prices. When disabled (B2C), they remain as gross prices (incl. VAT).",
    "Standard-Preisliste für den Produkt-Export. Wird als Basis für storefront-spezifische Preise verwendet und als Fallback, wenn keine Preisliste pro Storefront hinterlegt ist.":
        "Default price list for product export. Used as a baseline for storefront-specific prices and as a fallback when no per-storefront price list is set.",
    "Standard-Preisliste für den Produkt-Export zu Medusa.":
        "Default price list for product export to Medusa.",
    "Preisliste mit UVP / Streichpreisen. Wenn ein Artikel hier einen höheren Preis hat als in der Verkaufspreisliste, wird der UVP als durchgestrichener Preis (listPrice) an Shopware übertragen.":
        "Price list for MSRP / strike-through prices. When an item is priced higher here than in the sales price list, the MSRP is pushed to Shopware as listPrice (strike-through).",
    "Preisliste mit UVP / Streichpreisen. Wenn ein Artikel hier einen höheren Preis hat als in der Verkaufspreisliste, wird der UVP als Streichpreis an Medusa übertragen.":
        "Price list for MSRP / strike-through prices. When an item is priced higher here than in the sales price list, the MSRP is pushed to Medusa as a strike-through price.",
    "Shopware-Rules automatisch anlegen": "Auto-create Shopware rules",
    "Für unterschiedliche Preise pro Storefront werden Shopware-Pricing-Rules automatisch erzeugt und an die Storefront gebunden.":
        "For per-storefront pricing, Shopware pricing rules are auto-created and bound to the storefront.",

    # ── Customers / VAT / orders ──
    "Wie eingehende Shopware-Bestellungen Kunden in ERPNext zugeordnet werden. Der Standardkunde wird verwendet, wenn ein Gast bestellt oder kein bestehender Kunde gefunden wird.":
        "How incoming Shopware orders are matched to ERPNext customers. The default customer is used for guest orders or when no existing customer matches.",
    "Wie eingehende Medusa-Bestellungen Kunden in ERPNext zugeordnet werden. Der Standardkunde wird verwendet, wenn ein Gast bestellt oder kein bestehender Kunde gefunden wird.":
        "How incoming Medusa orders are matched to ERPNext customers. The default customer is used for guest orders or when no existing customer matches.",
    "Kundengruppe, der aus Shopware übernommene Kunden zugeordnet werden.":
        "Customer group new customers from Shopware are assigned to.",
    "Kundengruppe, der aus Medusa übernommene Kunden zugeordnet werden.":
        "Customer group new customers from Medusa are assigned to.",
    "Wird verwendet für Gast-Bestellungen oder wenn die Kunden-Synchronisation fehlschlägt.":
        "Used for guest orders or when customer sync fails.",
    "Wird verwendet für Bestellungen, bei denen der Medusa-Kunde in ERPNext nicht gefunden wird.":
        "Used for orders where the Medusa customer cannot be found in ERPNext.",
    "Ersatz-Vorname für Rechnungsadresse": "Fallback first name for billing address",
    "Wird verwendet, wenn Shopware keinen Vornamen für die Rechnungsadresse mitsendet. Standard: 'Billing' (sprachneutral).":
        "Used when Shopware doesn't send a first name with the billing address. Default: 'Billing' (locale-neutral).",
    "Wie oft Bestellungen aus Shopware abgeholt werden und welche Nummernkreise für Aufträge, Lieferscheine und Rechnungen verwendet werden.":
        "How often orders are fetched from Shopware and which naming series are used for orders, delivery notes, and invoices.",
    "Wie oft Bestellungen aus Medusa abgeholt werden und welche Nummernkreise verwendet werden.":
        "How often orders are fetched from Medusa and which naming series are used.",
    "Wie oft Bestellungen aus Shopware geholt werden (in Minuten). Kleinere Werte = aktueller, aber mehr Last.":
        "How often orders are fetched from Shopware (in minutes). Smaller values = more current, but more load.",
    "Wie oft Bestellungen aus Medusa geholt werden (in Minuten).":
        "How often orders are fetched from Medusa (in minutes).",
    "Wie oft Kunden aus Medusa geholt werden.": "How often customers are fetched from Medusa.",
    "Wie oft Zahlungsstatus aus Medusa geprüft wird.":
        "How often payment status is checked in Medusa.",
    "Nummernkreis Auftragsbestätigung": "Order confirmation naming series",
    "Nummernkreis, aus dem Auftragsbestätigungen für Shopware-Bestellungen vergeben werden.":
        "Naming series for order confirmations created from Shopware orders.",
    "Nummernkreis, aus dem Auftragsbestätigungen für Medusa-Bestellungen vergeben werden.":
        "Naming series for order confirmations created from Medusa orders.",
    "Nummernkreis für Lieferscheine, die aus Shopware-Sendungen erstellt werden.":
        "Naming series for delivery notes created from Shopware shipments.",
    "Nummernkreis für Lieferscheine, die aus Medusa-Sendungen erstellt werden.":
        "Naming series for delivery notes created from Medusa shipments.",
    "Nummernkreis für Rechnungen, die aus Shopware-Zahlungen erstellt werden.":
        "Naming series for invoices created from Shopware payments.",

    # ── Tax / VAT / shipping items ──
    "Steuer-Vorlage (optional)": "Tax template (optional)",
    "Standard-Steuertemplate für Bestellungen, wenn kein spezifisches Template ermittelt werden kann.":
        "Default tax template for orders when no specific template can be determined.",
    "Standard-MwSt.-Satz für die Brutto/Netto-Umrechnung, wenn der konkrete Satz aus den Bestelldaten nicht ermittelt werden kann. Deutschland: 19 % (Regel), 7 % (ermäßigt).":
        "Default VAT rate for gross/net conversion when the specific rate cannot be derived from order data. Germany: 19 % (standard), 7 % (reduced).",
    "Standard-MwSt.-Satz für die Brutto/Netto-Umrechnung, wenn kein passendes Steuertemplate gefunden wird. Deutschland: 19 % (Regel), 7 % (ermäßigt).":
        "Default VAT rate for gross/net conversion when no matching tax template is found. Germany: 19 % (standard), 7 % (reduced).",
    "Welches Steuertemplate und Versandkostenkonto für Medusa-Bestellungen verwendet wird.":
        "Which tax template and shipping account is used for Medusa orders.",
    "Auffangkonto, wenn für einen MwSt.-Satz keine spezifische Zuordnung gefunden wird.":
        "Fallback account when no specific VAT-rate mapping is found.",
    "Auffangland für Adressen ohne Länder-Code in Medusa.":
        "Fallback country for addresses without a country code in Medusa.",
    "ERPNext-Artikel für Versandkosten (z.B. 'VERSAND' oder 'SHIPPING'). Wird als Position eingefügt, sobald Versandkosten anfallen.":
        "ERPNext item for shipping costs (e.g. 'SHIPPING'). Inserted as a line item whenever shipping costs apply.",
    "ERPNext-Artikel für Versandkosten (z.B. 'VERSAND').":
        "ERPNext item for shipping costs (e.g. 'SHIPPING').",
    "Versandkosten als Auftragsposition erfassen": "Record shipping as a line item",
    "Empfohlen: aktiviert. Fügt Versandkosten als eigene Position in den Auftrag ein. Voraussetzung, damit Versandkosten aus Shopware übernommen werden.":
        "Recommended: enabled. Adds shipping as its own line item on the order. Required for shipping costs to be picked up from Shopware.",
    "Fügt Versandkosten als eigene Position in den Auftrag ein. Voraussetzung, damit Versandkosten aus Medusa übernommen werden.":
        "Adds shipping as its own line item on the order. Required for shipping costs to be picked up from Medusa.",
    "Wie Versandkosten aus Shopware-Bestellungen in ERPNext erfasst werden. Empfohlen: als separate Position über einen Dienstleistungs-Artikel (z.B. 'VERSAND').":
        "How shipping costs from Shopware orders are recorded in ERPNext. Recommended: as a separate line item via a service product (e.g. 'SHIPPING').",
    "Artikel für Rabatte": "Discount item",
    "Dummy-Artikel (z.B. 'RABATT'), über den Promo-Rabatte aus Shopware in den Auftrag eingefügt werden. Wird mit negativem Betrag erfasst.":
        "Dummy item (e.g. 'DISCOUNT') used to insert Shopware promo discounts into the order. Recorded with a negative amount.",
    "Dummy-Artikel (z.B. 'RABATT'), über den Promo-Rabatte aus Medusa in den Auftrag eingefügt werden. Wird mit negativem Betrag erfasst.":
        "Dummy item (e.g. 'DISCOUNT') used to insert Medusa promo discounts into the order. Recorded with a negative amount.",
    "Kostenstelle für Erlöse aus Shopware-Bestellungen.":
        "Cost center for revenue from Shopware orders.",
    "Kostenstelle für Erlöse aus Medusa-Bestellungen.":
        "Cost center for revenue from Medusa orders.",

    # ── Document automation ──
    "Welche Folgebelege automatisch erstellt werden (Lieferschein, Rechnung). Vor dem Aktivieren prüfen, dass Nummernkreise und Druckformate korrekt eingerichtet sind.":
        "Which follow-on documents are auto-created (delivery note, invoice). Before enabling, check that naming series and print formats are configured.",
    "Wenn aktiviert: Sobald in Shopware eine Sendung erstellt wird, legt ERPNext automatisch einen Lieferschein an. Deaktiviert: Lieferscheine müssen manuell erstellt werden.":
        "When enabled: ERPNext automatically creates a delivery note as soon as a shipment is created in Shopware. Disabled: delivery notes must be created manually.",
    "Rechnungen für bezahlte Bestellungen erstellen": "Create invoices for paid orders",
    "Erstellt eine Rechnung, sobald die Zahlung in Shopware bestätigt ist. Bei Versandhandel mit Vorkasse: oft die zweite Stufe nach 'Rechnung bei Versand'.":
        "Creates an invoice as soon as payment is confirmed in Shopware. For prepay mail-order: often the second step after 'Invoice on shipment'.",
    "Erstellt eine Rechnung, sobald die Zahlung in Medusa bestätigt ist.":
        "Creates an invoice as soon as payment is confirmed in Medusa.",
    "Erstellt automatisch eine Rechnung, sobald der Lieferschein gebucht wird. Rechnungsdatum = Versanddatum (Lieferdatum). WICHTIG: Wenn deaktiviert, müssen Sie Rechnungen manuell erstellen.":
        "Auto-creates an invoice as soon as the delivery note is submitted. Invoice date = shipment date. IMPORTANT: When disabled, you must create invoices manually.",
    "Konform mit deutschem Recht: erstellt automatisch eine Rechnung, sobald der Lieferschein gebucht wird. Rechnungsdatum = Versanddatum (Lieferdatum). WICHTIG: Wenn deaktiviert, müssen Sie Rechnungen manuell erstellen.":
        "Compliant with German law: auto-creates an invoice as soon as the delivery note is submitted. Invoice date = shipment date. IMPORTANT: When disabled, you must create invoices manually.",
    "Verschickt die Rechnung als PDF an die Kunden-E-Mail-Adresse, sobald die Zahlung bestätigt ist. Vor dem Aktivieren das Druckformat und ggf. die Vorlage prüfen.":
        "Sends the invoice as a PDF to the customer's email as soon as payment is confirmed. Check the print format and email template before enabling.",
    "Druckformat, das für den PDF-Anhang der Rechnung verwendet wird.":
        "Print format used for the invoice PDF attachment.",
    "Optionale E-Mail-Vorlage. Ohne Auswahl wird ein neutraler deutscher Standardtext verwendet.":
        "Optional email template. Without a selection, a neutral default text is used.",
    "E-Mail-Vorlage Rechnung": "Invoice email template",

    # ── Catalog / category sync ──
    "Kategorie-Sync (ERP → Shop)": "Category sync (ERP → shop)",
    "Konfigurierbare Produkt-Syncs (Ecommerce Product Sync). Ein Eintrag pro Scope (Storefront × Artikelgruppe / Filter). Vorschau, Sandbox-Push und Apply Live laufen jeweils gegen den Sync — nicht gegen diese Einstellungen.":
        "Configurable Product Syncs (Ecommerce Product Sync). One entry per scope (storefront × item group / filter). Preview, sandbox push, and Apply Live all run against the Sync — not against these settings.",
    "Konfigurierbare Produkt-Syncs (Ecommerce Product Sync). Ein Eintrag pro Scope (Verkaufskanal × Artikelgruppe / Filter). Vorschau, Sandbox-Push und Apply Live laufen jeweils gegen den Sync — nicht gegen diese Einstellungen.":
        "Configurable Product Syncs (Ecommerce Product Sync). One entry per scope (sales channel × item group / filter). Preview, sandbox push, and Apply Live all run against the Sync — not against these settings.",
    "Kategorie-Sync (Alt-Einstellungen)": "Category sync (legacy settings)",
    "Alte globale Kategorie-Sync-Einstellungen. Für neue Setups bitte den modernen Kategorie-Sync (oben) verwenden. Diese Werte werden nur als Fallback genutzt.":
        "Legacy global category-sync settings. For new setups please use the modern category sync (above). These values are used only as a fallback.",
    "Spiegelt den ERPNext-Artikelgruppen-Baum 1:1 in die Shopware-Kategorien. Ein Eintrag pro Storefront × Wurzel-Artikelgruppe.":
        "Mirrors the ERPNext item-group tree 1:1 into Shopware categories. One entry per storefront × root item group.",
    "Spiegelt den ERPNext-Artikelgruppen-Baum 1:1 in die Medusa-Produktkategorien. Ein Eintrag pro Wurzel-Artikelgruppe.":
        "Mirrors the ERPNext item-group tree 1:1 into Medusa product categories. One entry per root item group.",
    "Wurzel-Artikelgruppe für Kategorie-Sync": "Root item group for category sync",
    "Wurzelkategorie für den Shopware-Sync. Alle Unterkategorien darunter werden synchronisiert.":
        "Root category for the Shopware sync. All sub-categories below are synced.",
    "Wurzel-Artikelgruppe, ab der der Kategorie-Baum nach Medusa gespiegelt wird.":
        "Root item group from which the category tree is mirrored to Medusa.",
    "Geschützte Shopware-Kategorie-IDs": "Protected Shopware category IDs",
    "Shopware-Kategorie-IDs (eine pro Zeile), die nie automatisch gelöscht werden — auch nicht wenn sie 'verwaist' wirken. Diese Kategorien und ihre Unterkategorien sind geschützt. Sinnvoll für Kategorien, die direkt in Shopware gepflegt werden.":
        "Shopware category IDs (one per line) that are never auto-deleted, even if they look 'orphaned'. These categories and their sub-categories are protected. Useful for categories maintained directly in Shopware.",
    "Leere Kategorien synchronisieren": "Sync empty categories",
    "Wenn aktiviert: Auch Kategorien ohne zugeordnete Artikel werden zu Shopware übertragen.":
        "When enabled: categories without items are still pushed to Shopware.",
    "Leere Blatt-Kategorien synchronisieren": "Sync empty leaf categories",
    "Wenn deaktiviert: Blatt-Artikelgruppen ohne Artikel werden übersprungen. Wenn aktiviert: auch leere Blatt-Kategorien werden im Shop angelegt.":
        "When disabled: leaf item-groups without items are skipped. When enabled: empty leaf categories are created in the shop too.",
    "Wurzel-Kategorie nicht mitsynchronisieren": "Don't sync the root category itself",
    "Wenn aktiviert: die Wurzelkategorie selbst wird nicht in Shopware angelegt — nur ihre Kinder.":
        "When enabled: the root category itself isn't created in Shopware — only its children.",
    "Kategorie-Sync beim Komplett-Sync überspringen": "Skip category sync during full sync",
    "Wenn aktiviert, überspringt der Komplett-Sync Phase 1 (Kategorien) und synchronisiert nur die Artikel. Sinnvoll, wenn die Kategorien bereits aktuell sind.":
        "When enabled, the full sync skips phase 1 (categories) and only syncs items. Useful when the categories are already up to date.",
    "Verwaiste Kategorien": "Orphan categories",
    "Was passieren soll mit Backend-Produkten, die nicht (mehr) zur Auswahl gehören. <b>keep</b> = unverändert. <b>deactivate</b> = im Backend deaktivieren. <b>delete</b> = löschen (gefährlich). <b>report</b> = nur melden.":
        "What to do with backend products that no longer match the scope. <b>keep</b> = unchanged. <b>deactivate</b> = deactivate in the backend. <b>delete</b> = delete (dangerous). <b>report</b> = report only.",
    "Was mit Backend-Kategorien unter der Wurzel passieren soll, denen keine ERPNext-Artikelgruppe (mehr) entspricht. <strong>Beibehalten</strong> = unverändert lassen. <strong>Nur melden</strong> = in der Vorschau auflisten, aber nicht löschen. <strong>Löschen (gefährlich)</strong> = automatisch im Shop löschen — Vorsicht bei manuell gepflegten Kategorien!":
        "What to do with backend categories under the root that no longer match any ERPNext item group. <strong>Keep</strong> = leave alone. <strong>Report only</strong> = list in the preview but don't delete. <strong>Delete (dangerous)</strong> = auto-delete in the shop — caution with manually-maintained categories!",
    "<strong>Item Group Mapping</strong> (Standard): Jeder Artikel wird automatisch in die Medusa-Kategorie eingehängt, die seiner ERPNext-Artikelgruppe entspricht — plus eventuell Eigene Kollektionen. <strong>Smart Collections Only</strong>: Nur Eigene Kollektionen bestimmen die Kategorien — die Artikelgruppen werden nicht parallel nach Medusa gespiegelt. Letzteres wählen, wenn Sie Ihren Medusa-Kategorie-Baum ausschließlich über Eigene Kollektionen pflegen wollen.":
        "<strong>Item Group Mapping</strong> (default): each item is auto-linked to the Medusa category corresponding to its ERPNext item group — plus any Smart Collections. <strong>Smart Collections Only</strong>: only Smart Collections drive categories — the item-group tree is not mirrored to Medusa. Pick this if you maintain your Medusa category tree exclusively via Smart Collections.",
    "Optionale Jinja-Vorlagen, die bestimmen, wie ERPNext-Artikelgruppen im Shop benannt und beschrieben werden.":
        "Optional Jinja templates controlling how ERPNext item groups are named and described in the shop.",
    "Namens-Vorlagen": "Name templates",
    "Optionale Jinja-Vorlage für die Backend-Kategorie-Beschreibung.":
        "Optional Jinja template for the backend category description.",
    "Jinja-Template für den Kategorie-Namen im Backend. Default <code>{{ item_group.item_group_name }}</code> übernimmt den Artikelgruppen-Namen unverändert.":
        "Jinja template for the backend category name. Default <code>{{ item_group.item_group_name }}</code> uses the item-group name as-is.",
    "Jinja-Template für den Produkt-Namen im Backend.":
        "Jinja template for the product name in the backend.",
    "UUID der bestehenden Backend-Kategorie, die als Wurzel dient. Bleibt leer wenn neu angelegt werden soll — wird beim ersten Sync automatisch aus der Storefront ermittelt.":
        "UUID of the existing backend category to use as root. Leave empty to create it — derived from the storefront automatically on first sync.",

    # ── Smart collections (rest) ──
    "Regeln": "Rules",
    "Filter-Regeln": "Filter rules",
    "Definieren, welche Artikel automatisch in diese Kollektion fallen. Jede Zeile ist eine Regel — z.B. 'Artikelgruppe ist Schuhe' oder 'Marke enthält Nike'.":
        "Define which items fall into this collection automatically. Each row is a rule — e.g. 'Item group is Shoes' or 'Brand contains Nike'.",
    "Regel-Verknüpfung": "Rule combinator",
    "<strong>AND</strong> verknüpft alle Regeln (alle müssen zutreffen). <strong>OR</strong> reicht aus, dass eine Regel zutrifft. OR-Gruppen sind über die Spalte 'OR Group' möglich (Zeilen mit gleicher OR-Group-Nummer werden mit OR kombiniert, die Gruppen untereinander dann nach diesem Kombinator).":
        "<strong>AND</strong> requires every rule to match. <strong>OR</strong> requires one. OR-groups are supported via the 'OR Group' column (rows sharing the same OR-group number are OR-combined; the groups themselves combine via this combinator).",
    "In welche Shops/Storefronts diese Kollektion synchronisiert wird. Pro Storefront eine Zeile anlegen.":
        "Which shops/storefronts this collection is synced to. One row per storefront.",
    "Kehrt die Regel um. Hinweis: NULL-Behandlung ist streng — ein nicht gesetzter Wert passt nie, daher ist NICHT(Regel) nicht identisch mit der Operator-Verneinung, wenn die Eigenschaft fehlt.":
        "Inverts the rule. Note: NULL handling is strict — an unset value never matches, so NOT(rule) is not identical to the operator-negated form when the property is missing.",
    "Schreibgeschützte Informationen zum letzten Auflösen der Regeln.":
        "Read-only information from the last rule resolution.",
    "Anzahl Artikel, die beim letzten Sync von den Regeln erfasst wurden.":
        "Number of items the rules captured on the last sync.",
    "Zeitpunkt des letzten Regel-Durchlaufs.":
        "Timestamp of the last rule run.",
    "ID der Backend-Kategorie. Wird nach dem ersten erfolgreichen Sync automatisch gefüllt.":
        "Backend category ID. Auto-filled after the first successful sync.",
    "Zeitpunkt des letzten erfolgreichen Syncs für dieses Ziel.":
        "Timestamp of the last successful sync for this target.",
    "Wird zu Beginn jedes Sync-Versuchs aktualisiert. Der Stale-Target-Recover entfernt Einträge, die zu lange im Status 'running' hängen.":
        "Updated at the start of every sync attempt. Stale-target-recover removes rows stuck in 'running' for too long.",
    "Wird zu Beginn jedes Sync-Versuchs aktualisiert. Der Stale-Mirror-Recover entfernt Einträge, die zu lange im Status 'running' hängen.":
        "Updated at the start of every sync attempt. Stale-mirror-recover removes rows stuck in 'running' for too long.",
    "Letzte Fehlermeldung (informativ)": "Last error message (informational)",
    "Letzte Fehlermeldung": "Last error message",
    "Kurzfassung; vollständiger Stacktrace im Ecommerce Integration Log.":
        "Short summary; full stacktrace in the Ecommerce Integration Log.",
    "Kurzfassung der letzten Fehlermeldung. Vollständiger Stacktrace im Ecommerce Integration Log.":
        "Short summary of the most recent error. Full stacktrace in the Ecommerce Integration Log.",
    "Regelbasierte Kollektionen, die sich nicht aus dem Artikelgruppen-Baum ergeben (z.B. Sale, Bestseller, saisonale Aktionen).":
        "Rule-based collections that don't fall out of the item-group tree (e.g. Sale, Bestsellers, seasonal promotions).",
    "Wann der Sync automatisch läuft.": "When the sync runs automatically.",
    "Manuelle Zuordnungen pro Artikelgruppe (Backend-UUID fest verknüpfen oder Artikelgruppe komplett ausnehmen).":
        "Manual overrides per item group (pin a backend UUID or exclude the item group entirely).",
    "Knoten-Überschreibungen": "Node overrides",
    "Sonderfälle: einzelne Artikelgruppen einer bestimmten Backend-Kategorie zuordnen oder ganz vom Sync ausnehmen.":
        "Special cases: map specific item groups to a specific backend category or exclude them from the sync.",
    "Sonderfälle pro Artikel: bestehendes Backend-Produkt manuell verknüpfen, Artikel überspringen oder Felder überschreiben.":
        "Special cases per item: manually link an existing backend product, skip an item, or override individual fields.",
    "Schreibgeschützte Status-Informationen zum letzten Sync. Nur zur Information.":
        "Read-only status info from the last sync. Informational only.",
    "Zeitpunkt des letzten erfolgreichen Sync-Laufs.":
        "Timestamp of the last successful sync run.",
    "Letzter Bestand geholt am": "Last stock pull at",
    "Letzte Bestellung geholt am": "Last order pull at",
    "Letzter Kunde geholt am": "Last customer pull at",
    "Anzahl Artikel (zuletzt aufgelöst)": "Item count (last resolved)",
    "Anzahl Artikel pro Sync-Stapel. Größere Werte = schneller, aber mehr RAM-Bedarf.":
        "Number of items per sync batch. Larger values = faster but more RAM.",
    "Anzahl Vorlage-Artikel mit Varianten pro Stapel.":
        "Number of template items with variants per batch.",
    "Stapelgröße": "Batch size",
    "Stapelgröße Artikel": "Item batch size",
    "Stapelgröße Bilder": "Image batch size",
    "Stapelgröße Preise": "Price batch size",
    "Stapelgröße Varianten": "Variant batch size",
    "Stapelgrößen beim Komplett-Sync": "Batch sizes for the full sync",
    "Optionen für den manuellen Komplett-Abgleich aller Daten. Diese Einstellungen werden nur beim Komplett-Sync verwendet, nicht beim laufenden Betrieb.":
        "Options for the manual full reconciliation. These settings only apply to the full sync, not regular operation.",
    "Komplett-Sync (Erweitert)": "Full sync (advanced)",
    "Vorlage-Artikel (Eltern) inaktiv exportieren": "Export template (parent) items as inactive",
    "Empfohlen: aktiviert. Vorlage-Artikel mit Varianten werden in Shopware inaktiv angelegt; nur die Varianten sind im Shop sichtbar. Verhindert doppelte Treffer in der Produktliste.":
        "Recommended: enabled. Template items with variants are created inactive in Shopware; only the variants are visible. Prevents duplicate hits in the product list.",
    "Feld-Zuordnungen (Erweitert)": "Field mappings (advanced)",
    "Welche ERPNext-Artikel-Felder zu Shopware übertragen werden — und ob als Custom Field (nicht filterbar) oder als Property (filterbar im Shop).":
        "Which ERPNext item fields are pushed to Shopware — and whether as a Custom Field (not filterable) or as a Property (filterable in the shop).",
    "Wie Shopware-Checkout-Felder (Custom Fields aus dem Bestellprozess) auf ERPNext abgebildet werden. Pro Feld eine Zeile anlegen.":
        "How Shopware checkout fields (custom fields from the checkout flow) are mapped to ERPNext. One row per field.",
    "Checkout-Felder (Erweitert)": "Checkout fields (advanced)",
    "Wie Medusa-Order-Metadata-Keys auf ERPNext-Felder oder Dienstleistungs-Artikel abgebildet werden. Pro Feld eine Zeile anlegen.":
        "How Medusa order metadata keys are mapped to ERPNext fields or service items. One row per field.",
    "Kategorie-Zuordnung": "Category mapping",

    # ── Pull Sync rest ──
    "Eingehende Synchronisationen (Bestellungen / Kunden / Bestand von Shopware nach ERPNext). Pro Pull-Sync ein Cron-Intervall; manuelle Ausführung über die Schaltfläche unten.":
        "Incoming syncs (orders / customers / stock from Shopware to ERPNext). One cron interval per Pull Sync; manual run via the button below.",
    "Eingehende Synchronisationen (Bestellungen / Kunden / Bestand von Medusa nach ERPNext). Pro Pull-Sync ein Cron-Intervall; manuelle Ausführung über die Schaltfläche unten.":
        "Incoming syncs (orders / customers / stock from Medusa to ERPNext). One cron interval per Pull Sync; manual run via the button below.",
    "Welche Entitäten beim Pull aus dem Backend abgeholt werden. Pro Run wird jede aktivierte Sektion mit ihrer Watermark abgefragt.":
        "Which entities are pulled from the backend. Per run, each enabled section is queried using its watermark.",
    "Default 0 — meist ist ERPNext die Stock-Quelle. Aktivieren nur, wenn das Backend die Wahrheit ist (z.B. POS-System).":
        "Default 0 — usually ERPNext is the source of stock truth. Enable only when the backend is the source of truth (e.g. POS).",
    "Rückblick (Stunden)": "Lookback (hours)",
    "Beim Erstlauf ohne Watermark wird so weit zurückgegriffen. Default 7 Tage.":
        "On the first run without a watermark, this is how far back to look. Default 7 days.",
    "Auf welche Storefronts der Pull beschränkt wird. Leer = alle Channels.":
        "Which storefronts the pull is restricted to. Empty = all channels.",
    "Pro Entität: Zeitpunkt des letzten erfolgreichen Pulls. Pulls fragen alles ab, was seither erstellt/geändert wurde.":
        "Per entity: timestamp of the last successful pull. Pulls cover anything created/changed since.",
    "Geholt insgesamt (letzter Run)": "Total fetched (last run)",
    "Erfolgreich synchronisiert": "Synced successfully",
    "Zeitpunkt des letzten erfolgreichen Bestellungs-Abrufs. Nur zur Information.":
        "Timestamp of the last successful order pull. Informational only.",
    "Zeitpunkt des letzten erfolgreichen Kunden-Abrufs. Nur zur Information.":
        "Timestamp of the last successful customer pull. Informational only.",
    "Zeitpunkt des letzten erfolgreichen Bestand-Syncs. Nur zur Information.":
        "Timestamp of the last successful stock sync. Informational only.",
    "Zeitpunkt des letzten erfolgreichen Zahlungs-Abrufs. Nur zur Information.":
        "Timestamp of the last successful payment pull. Informational only.",

    # ── Sync Run / Sync Error rest ──
    "AI-Beschreibungen erzeugt": "AI descriptions generated",
    "Wie viele Items im Pre-Pass über die AI-Description-Pipeline einen Text bekommen haben (nur wenn description_source=ai_generated und auto_generate_ai_descriptions aktiv).":
        "How many items received a description via the AI pipeline in the pre-pass (only when description_source=ai_generated and auto_generate_ai_descriptions is on).",
    "Fehler-Zusammenfassung": "Error summary",
    "Erste paar Fehler dieses Laufs. Vollständige Liste siehe verknüpfte Ecommerce Sync Error-Einträge.":
        "First few errors of this run. See linked Ecommerce Sync Error rows for the full list.",
    "Vollständiger ProductSyncPreviewPlan zum Zeitpunkt des Laufs. Basis für Run-Vergleich und Export.":
        "Full ProductSyncPreviewPlan as of the run. Used for run-compare and export.",
    "Tatsächlich angewendete Änderungen (JSON)": "Actually applied changes (JSON)",
    "Tatsächlich getroffener Channel": "Actually hit channel",
    "Nach echtem Apply: pro Item die Liste der angewendeten FieldDiffs (action, item_code, field, vorher, nachher). Basis für die 'Was hat Run X bei Item Y geändert'-Drill-Down-Ansicht. Bei dry-run/preflight leer.":
        "After a real apply: per-item list of applied FieldDiffs (action, item_code, field, before, after). Backs the 'what did run X change on item Y' drill-down view. Empty for dry-run / preflight.",
    "Nur bei mode=preflight gefüllt — Liste der Findings (severity, code, message, fix_hint).":
        "Only populated for mode=preflight — list of findings (severity, code, message, fix_hint).",
    "Pre-Apply-Snapshot der betroffenen Backend-Produkte. Ermöglicht 'Rollback diesen Run' bei Bedarf. Wird nur bei Live-Apply mit Snapshot-Option befüllt.":
        "Pre-apply snapshot of the affected backend products. Backs 'rollback this run' on demand. Only populated for Live Apply with the snapshot option.",
    "Gelöscht": "Deleted",
    "Übersprungen": "Skipped",
    "Mindestbestand für Push": "Minimum stock for push",
    "Cron-Voreinstellung": "Cron preset",
    "Was an das Backend geschickt wurde. Hilfreich beim Reproduzieren des Fehlers.":
        "What was sent to the backend. Useful for reproducing the error.",
    "Was das Backend zurückgegeben hat. Enthält i.d.R. die genaue Validierungsfehler-Meldung.":
        "What the backend responded with. Usually contains the precise validation error.",
    "Fehler-Details": "Error details",
    "Fehlermeldung": "Error message",

    # ── Product Sync labels ──
    "Auswahl der Artikel": "Item scope",
    "Auswahl-Modus": "Scope mode",
    "Verknüpfter Kategorie-Sync": "Linked category sync",
    "Verknüpfte Kollektion (Legacy)": "Linked collection (legacy)",
    "Verknüpfte Kollektionen": "Linked collections",
    "Wenn ein Artikel in mehreren aktiven Syncs derselben Priorität ist.":
        "When an item appears in multiple active syncs at the same priority.",
    "Beschreibungs-Quelle": "Description source",
    "Beschreibungs-Vorlage": "Description template",
    "Namens-Vorlage": "Name template",
    "Wie Name/Beschreibung/Preis im Backend gebildet werden.":
        "How name / description / price are produced in the backend.",
    "Granular pro Feldgruppe ein-/ausschaltbar. Hilfreich um z.B. Bilder bei Erstsync zu pausieren und später nachzuziehen.":
        "Toggleable per field group. Useful e.g. to pause images on the first sync and catch them up later.",
    "Stammdaten (Name, SKU, Beschreibung)": "Basic data (name, SKU, description)",
    "Beschreibung": "Description",
    "Wie der Sync mit nicht (mehr) passenden Artikeln oder Konflikten umgeht.":
        "How the sync handles items that no longer match or that conflict.",
    "Eine oder mehrere Smart Collections — alle in Auswahl enthaltenen Artikel landen im Sync. Pro Zeile optional eine Storefront-ID filtern, damit die Kollektion nur an einen Channel gepushed wird.":
        "One or more Smart Collections — every item in scope lands in the sync. Optional per-row storefront filter pins a collection to a single channel.",
    "Altfeld — wurde durch die Tabelle 'Verknüpfte Kollektionen' ersetzt. Inhalt wird beim Patch in die Tabelle migriert; dieses Feld bleibt für Rückwärtskompatibilität erhalten.":
        "Legacy field — replaced by the 'Linked Collections' table. Content is migrated by the patch; this field remains for backwards compatibility.",
    "Artikel in Auswahl (letzter Lauf)": "Items in scope (last run)",
    "Wird gerendert, wenn das jeweilige Feld am Item leer ist. Variablen: {{ item }}, {{ sync }}, {{ brand }} (Channel-Branding falls vorhanden). Beispiele: \\'{{ item.item_name }} | {{ brand.brand_name or item.brand or \"\" }}\\' für Meta-Title.":
        "Rendered when the corresponding Item field is empty. Variables: {{ item }}, {{ sync }}, {{ brand }} (channel branding if available). Example: \\'{{ item.item_name }} | {{ brand.brand_name or item.brand or \"\" }}\\' for the meta title.",
    "Fallback für item.meta_title. Beispiel: {{ item.item_name }} | {{ item.brand or '' }}":
        "Fallback for item.meta_title. Example: {{ item.item_name }} | {{ item.brand or '' }}",
    "Fallback für item.meta_description. Renderfehler werden als leerer String behandelt, damit ein kaputtes Template den Sync nicht stoppt.":
        "Fallback for item.meta_description. Render errors collapse to an empty string so a broken template can't stop the sync.",
    "Fallback für item.seo_slug / item.slug. Wird beim Rendern automatisch slugified.":
        "Fallback for item.seo_slug / item.slug. Slugified automatically on render.",
    "Meta-Title-Vorlage": "Meta title template",
    "Meta-Description-Vorlage": "Meta description template",
    "Slug-Vorlage": "Slug template",
    "Fehlende AI-Beschreibungen vor dem Sync erzeugen":
        "Generate missing AI descriptions before sync",
    "Vor jedem Lauf einmalig generate_descriptions_batch() für Items aufrufen, die noch keine AI-Beschreibung haben. Begrenzt durch max_ai_pre_generate (Default 100) pro Lauf, damit ein einzelner Sync das Token-Budget nicht leerschöpft.":
        "Before each run, call generate_descriptions_batch() once for items without an AI description. Capped at max_ai_pre_generate (default 100) per run so a single sync can't exhaust the token budget.",
    "Obergrenze für die Pre-Pass-Generierung pro Sync-Lauf. 0 = unbegrenzt (Vorsicht: jeder Eintrag kostet Gemini-Tokens).":
        "Cap for AI generations per sync run. 0 = unlimited (caution: each generation costs Gemini tokens).",
    "Wie viele Datensätze pro Durchlauf verarbeitet werden. Größere Werte = schneller, aber mehr RAM-Bedarf. Standard ist meist passend.":
        "How many rows per pass. Larger values = faster but more RAM. The default is usually fine.",
    "Wie Bilder ins Backend übertragen werden.":
        "How images are pushed to the backend.",
    "<b>url_with_bytes_fallback</b> = bevorzugt URL-Referenz, lädt Binary nur hoch, wenn die URL als intern erkannt wird. <b>url_only</b> = immer URL. <b>bytes_only</b> = immer Binary.":
        "<b>url_with_bytes_fallback</b> = prefer URL reference, only upload binaries when the URL is recognised as internal. <b>url_only</b> = always URL. <b>bytes_only</b> = always binary.",
    "Ein Muster pro Zeile, z.B. <code>*.internal</code>, <code>192.168.*</code>, <code>erp.local</code>. Bei Treffer wird das Bild als Binary hochgeladen.":
        "One pattern per line, e.g. <code>*.internal</code>, <code>192.168.*</code>, <code>erp.local</code>. On match, the image is uploaded as a binary.",
    "Sales-Channel-UUID, die der Sandbox-Push-Dialog vorauswählt. Muss in den Sales-Channel-Settings is_sandbox=1 tragen, sonst wird sie ignoriert.":
        "Sales-channel UUID pre-selected by the sandbox-push dialog. Must have is_sandbox=1 in the sales-channel settings, otherwise it's ignored.",
    "Optionaler Beschreibungstext, der im Shop und für SEO verwendet wird.":
        "Optional description used in the shop and for SEO.",
    "Optionaler Text für Storefront / SEO.": "Optional text for storefront / SEO.",

    # ── Catalog Mirror rest ──
    "Wurzel": "Root",
    "Wurzel-Artikelgruppe": "Root item group",
    "Backend-Wurzel-Kategorie-ID": "Backend root category ID",
    "Welche ERPNext-Artikelgruppe als Ausgangspunkt für die Spiegelung verwendet wird, und welche Backend-Kategorie ihr entspricht.":
        "Which ERPNext item group is the mirror's starting point, and which backend category corresponds to it.",
    "Die Spiegelung beginnt hier und folgt allen Unter-Artikelgruppen. Das 'Vom Kategorie-Sync ausnehmen'-Flag der Wurzel wird ignoriert.":
        "The mirror starts here and follows every sub-item-group. The root's 'exclude from category sync' flag is ignored.",
    "Kleinere Werte ranken höher. Entscheidet bei Konflikten, welche Kollektion eine Hauptkategorie 'gewinnt'.":
        "Lower values rank higher. Decides which collection 'wins' a main category on conflicts.",
    "Wenn deaktiviert (Standard): Wenn die Regeln keine Artikel ergeben, wird die Backend-Kategorie nicht angelegt und eine Warnung erscheint. Aktivieren, um auch leere Kollektionen anzulegen.":
        "When disabled (default): if the rules produce no items, the backend category isn't created and a warning appears. Enable to create empty collections too.",

    # ── Channel Branding (key items only) ──
    "Titel auf der Auftragsbestätigungs-PDF (nach Freigabe durch das Büro)":
        "Title on the order confirmation PDF (after office release)",
    "Titel auf der Bestellbestätigungs-PDF (wird dem Kunden sofort nach Bestelleingang geschickt)":
        "Title on the order receipt PDF (sent to the customer immediately after order entry)",
    "Einleitungssatz auf der Auftragsbestätigungs-PDF":
        "Intro sentence on the order confirmation PDF",
    "Einleitungssatz auf der Bestellbestätigungs-PDF":
        "Intro sentence on the order receipt PDF",
    "Betreffzeile der Auftragsbestätigungs-E-Mail. Jinja unterstützt (z.B. {{ doc.name }} für die Auftragsnummer).":
        "Subject line of the order confirmation email. Jinja supported (e.g. {{ doc.name }} for the order number).",
    "Betreffzeile der Bestellbestätigungs-E-Mail. Jinja unterstützt (z.B. {{ doc.name }} für die Auftragsnummer).":
        "Subject line of the order receipt email. Jinja supported (e.g. {{ doc.name }} for the order number).",
    "Betreffzeile der Rechnungs-E-Mail. Jinja unterstützt (z.B. {{ doc.name }} für die Rechnungsnummer).":
        "Subject line of the invoice email. Jinja supported (e.g. {{ doc.name }} for the invoice number).",
    "Anrede am Anfang der Auftragsbestätigungs-E-Mail. Jinja-Platzhalter erlaubt: {{ first_name }}, {{ last_name }}, {{ salutation }}, {{ gender }}, {{ customer_name }}, {{ auto_greeting }} (sprachabhängige Auto-Anrede mit Fallback auf 'Sehr geehrte Damen und Herren').":
        "Greeting at the top of the order confirmation email. Jinja placeholders allowed: {{ first_name }}, {{ last_name }}, {{ salutation }}, {{ gender }}, {{ customer_name }}, {{ auto_greeting }} (locale-aware auto-greeting with fallback to 'Dear Sir or Madam').",
    "Anrede am Anfang der Bestellbestätigungs-E-Mail. Jinja-Platzhalter erlaubt: {{ first_name }}, {{ last_name }}, {{ salutation }}, {{ gender }}, {{ customer_name }}, {{ auto_greeting }} (sprachabhängige Auto-Anrede mit Fallback auf 'Sehr geehrte Damen und Herren').":
        "Greeting at the top of the order receipt email. Jinja placeholders allowed: {{ first_name }}, {{ last_name }}, {{ salutation }}, {{ gender }}, {{ customer_name }}, {{ auto_greeting }} (locale-aware auto-greeting with fallback to 'Dear Sir or Madam').",
    "Hauptinhalt der Bestellbestätigungs-E-Mail (HTML, Jinja-Platzhalter unterstützt: {{ doc.name }}, {{ doc.grand_total }}, sowie {{ first_name }}, {{ last_name }}, {{ salutation }}, {{ gender }}, {{ auto_greeting }}).":
        "Body of the order receipt email (HTML, Jinja placeholders supported: {{ doc.name }}, {{ doc.grand_total }}, plus {{ first_name }}, {{ last_name }}, {{ salutation }}, {{ gender }}, {{ auto_greeting }}).",
    "Hauptinhalt der Rechnungs-E-Mail (HTML, Jinja-Platzhalter unterstützt).":
        "Body of the invoice email (HTML, Jinja placeholders supported).",

    # ── Misc ──
    "Wie viele Items im Pre-Pass über die AI-Description-Pipeline einen Text bekommen haben (nur wenn description_source=ai_generated und auto_generate_ai_descriptions aktiv).":
        "How many items received text via the AI description pipeline in the pre-pass (only when description_source=ai_generated and auto_generate_ai_descriptions is on).",
    "Zuletzt aufgelöst (informativ)": "Last resolved (informational)",

    # ── Final stragglers (escaped-quote and special-char strings) ──
    "Wird verwendet, wenn keine der obigen Zuordnungen passt. Ohne Auswahl wird die ERPNext-Standardzahlart verwendet.":
        "Used when none of the mappings above match. Without a selection, the ERPNext default payment method is used.",
    'Wird gerendert, wenn das jeweilige Feld am Item leer ist. Variablen: {{ item }}, {{ sync }}, {{ brand }} (Channel-Branding falls vorhanden). Beispiele: \'{{ item.item_name }} | {{ brand.brand_name or item.brand or "" }}\' für Meta-Title.':
        'Rendered when the corresponding Item field is empty. Variables: {{ item }}, {{ sync }}, {{ brand }} (channel branding if available). Example: \'{{ item.item_name }} | {{ brand.brand_name or item.brand or "" }}\' for the meta title.',
}
