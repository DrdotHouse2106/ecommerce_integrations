<div align="center">
    <img src="https://frappecloud.com/files/ERPNext%20-%20Ecommerce%20Integrations.png" height="128">
    <h2>Ecommerce Integrations für ERPNext</h2>

[![CI](https://github.com/frappe/ecommerce_integrations/actions/workflows/ci.yml/badge.svg)](https://github.com/frappe/ecommerce_integrations/actions/workflows/ci.yml)

**[English version / Englische Version](README.en.md)**

</div>

> **Hinweis:** Dies ist ein erweiterter Fork von [`frappe/ecommerce_integrations`](https://github.com/frappe/ecommerce_integrations) mit vollwertiger **Shopware 6**- und **Medusa v2**-Integration, einer gemeinsamen Delta-Sync-Engine, Katalog-Kategorisierung, KI-gestützter Produktbeschreibung und Vektorsuche sowie deutschen Rechnungs-/Bestellprozessen. Zielplattform ist **Frappe / ERPNext v16**.

## Unterstützte Integrationen

| Integration | Beschreibung | Doku |
|-------------|--------------|------|
| **Shopware 6** | Vollständige bidirektionale Synchronisation (Produkte, Bestellungen, Kunden, Status) | siehe unten |
| **Medusa v2** | Produkt-, Bestell-, Kunden- und Lagersynchronisation mit Medusa v2 (headless commerce) | siehe unten |
| **Product Sync Engine** | Gemeinsame Delta-Sync-Engine für Shopware & Medusa | [`docs/product_sync.md`](docs/product_sync.md) |
| **Catalog Mirror** | 1:1-Spiegelung der ERPNext-Artikelgruppen als Shop-Kategorien | [`docs/catalog_mirror.md`](docs/catalog_mirror.md) |
| **Smart Collections** | Regelbasierte Produktgruppen (Sale, Bestseller, Themenwelten …) | [`docs/smart_collections.md`](docs/smart_collections.md) |
| **RAG (Vektorsuche)** | Export der Produktdaten als Embeddings nach Pinecone für KI-Suchassistenten | siehe unten |
| **AI Description** | KI-generierte Produktbeschreibungen, Kurztexte und SEO-Texte via Google Gemini | siehe unten |
| Shopify | Shopify-Integration (unverändert von Upstream übernommen) | [User-Doku](https://docs.erpnext.com/docs/v13/user/manual/en/erpnext_integration/shopify_integration) |
| Unicommerce | Unicommerce-Integration (unverändert von Upstream übernommen) | [User-Doku](https://docs.erpnext.com/docs/v13/user/manual/en/erpnext_integration/unicommerce_integration) |
| Zenoti | Zenoti-Integration (unverändert von Upstream übernommen) | [User-Doku](https://docs.erpnext.com/docs/v13/user/manual/en/erpnext_integration/zenoti_integration) |
| Amazon | Amazon SP-API-Integration (unverändert von Upstream übernommen) | [User-Doku](https://docs.erpnext.com/docs/v13/user/manual/en/erpnext_integration/amazon_integration) |

---

## Shopware 6 Integration

Vollständige bidirektionale Anbindung zwischen ERPNext und Shopware 6:

**Produkte (ERPNext → Shopware)**
- Einfache Artikel und Varianten (inkl. Konfigurator/Optionen, Attribute)
- Basispreis (netto/brutto, je Preisliste konfigurierbar) sowie UVP/Streichpreis
- Lagerbestand je Artikel (Summierung über alle Lager)
- Bildergalerie inkl. Titelbild, automatischer Medien-Upload und -Abgleich
- Eigenschaften/Property-Groups mit Filterfunktion im Shop-Frontend, inkl. sortierbarer Reihenfolge
- Frei konfigurierbare Zusatzfelder (Shopware Custom Fields) — ein ERPNext-Feld wird zentral auf ein Shopware-Feld gemappt, kein Pflegeaufwand pro Artikel
- Marke/Hersteller inkl. Logo und Beschreibung
- Lieferzeit und Nachbestellzeit („Wiederauffüllzeit"), mit globalem Standardwert als Fallback
- SEO-Metadaten (Meta-Titel, Meta-Beschreibung)
- Kategorien über Catalog Mirror bzw. Smart Collections
- Mindestbestellmenge/Staffelmengen, Steuersätze
- Verkaufskanal-Sichtbarkeit je Artikel (inkl. manueller Ausnahmen)

**Produkte (Shopware → ERPNext, Rückimport)**
- Artikel, Varianten, Kategorien, Bilder, Preise, Bestand, Eigenschaften, Marke, Lieferzeit, Beschreibung, SEO- und Rabattfelder

**Bestellungen & Status**
- Bestellimport von Shopware nach ERPNext
- Rückmeldung des Auftragsstatus an Shopware bei Lieferschein-, Zahlungs- und Rechnungsbuchung

**Kunden**
- Kundenabgleich inkl. Leitweg-ID für die E-Rechnung (XRechnung)
- Einheitliche Kundennummern: ERPNext ist maßgeblich, Shopwares Kundennummer wird automatisch angeglichen — auch wenn sich ein Kunde selbst im Shop registriert
- Optional (Opt-in je Kunde): neue ERPNext-Kunden automatisch als Account (ohne Passwort, Aktivierung über „Passwort vergessen") nach Shopware übertragen

**Technik**
- Redis-Warteschlange für performante Massensynchronisation
- Läuft über die gemeinsame **Product Sync Engine** (siehe unten) mit Hash-basiertem Delta-Erkennung — unveränderte Artikel erzeugen keine API-Aufrufe

**Einrichtung:**
1. `Shopware Setting` in ERPNext öffnen
2. Zugangsdaten der Shopware-6-Admin-API hinterlegen
3. Lager- und Steuerzuordnung konfigurieren
4. Gewünschte Sync-Optionen aktivieren

---

## Medusa v2 Integration

Anbindung von ERPNext an Medusa v2 (headless commerce):

- **Produkte**: Export inkl. Preis (netto), Metadaten aus den Ecommerce-Eigenschaften, Marke, Bildergalerie
- **Bestellungen**: Import via Webhook oder geplanter Synchronisation
- **Kunden**: Bidirektionaler Abgleich
- **Lagerbestand**: Übertragung auf Medusa-Standorte
- **Status-Sync**: Rückmeldung von Versand- und Zahlungsstatus
- **Verkaufskanäle**: Zuordnung je Artikel
- Läuft ebenfalls über die gemeinsame **Product Sync Engine**

**Einrichtung:**
1. `Medusa Setting` in ERPNext öffnen
2. API-Schlüssel und Basis-URL der Medusa-v2-Instanz eintragen
3. Preislisten- und Lagerzuordnung konfigurieren
4. Gewünschte Sync-Optionen aktivieren

---

## Product Sync Engine

Die gemeinsame Delta-Sync-Engine für Shopware und Medusa (Dokumentart `Ecommerce Product Sync`):

- **Geltungsbereich** wählbar: gesamter Katalog, eine Artikelgruppe (mit/ohne Unterbaum), ein Catalog Mirror oder eine Smart Collection
- **Kanonischer Hash** pro Artikel — nur tatsächlich geänderte Felder werden gepusht; ein unveränderter Katalog erzeugt bei jedem Lauf null Backend-Schreibzugriffe
- **Vorschau mit Feld-Diff**: vor dem Live-Push zeigt eine Vorschau genau, was sich ändern würde (inkl. Risikohinweisen bei großen Preis-/Bestandssprüngen)
- **Zeitplan** je Sync (stündlich, alle 6 Stunden, täglich, individueller Cron-Ausdruck) oder rein manuell
- **Priorisierung** bei überlappenden Syncs sowie Konfliktauflösung (manuelle Prüfung, "zuletzt gewinnt", überspringen)
- **Pro-Artikel-Overrides**: einzelne Artikel lassen sich anheften, überspringen oder mit abweichendem Namen/Preis/Beschreibung ausstatten
- Bulk-Push in Batches über die jeweilige Backend-API, mit deterministischen IDs für wiederholbare, idempotente Läufe
- Ausführliches Audit-Log je Lauf (`Ecommerce Sync Run`, `Ecommerce Sync Error`)

---

## Catalog Mirror & Smart Collections

Die Kategorie-Zuordnung im Backend ist auf zwei sich ergänzende Module aufgeteilt.

**Catalog Mirror** hält eine 1:1-Spiegelung des ERPNext-Artikelgruppenbaums unterhalb eines Wurzelknotens im Backend (Shopware oder Medusa). Ein Mirror pro (Backend, Wurzel-Artikelgruppe); Umbenennungen, Verschiebungen und neue Artikelgruppen werden beim nächsten Sync automatisch übernommen. Sinnvoll, wenn die reguläre Shop-Kategoriestruktur der ERPNext-Struktur folgen soll. Siehe [`docs/catalog_mirror.md`](docs/catalog_mirror.md).

**Smart Collections** sind regelbasierte, freie Gruppierungen — `Sale`, `Bestseller`, Themenwelten — die nicht dem Artikelgruppenbaum folgen. Die Regeln einer Collection lösen sich zu einer Artikelmenge auf; jedes aktivierte Ziel schiebt diese Artikel auf einen Verkaufskanal (Shopware) oder eine Produktkategorie (Medusa). Siehe [`docs/smart_collections.md`](docs/smart_collections.md).

Ausnahmen auf Artikelebene liegen auf `Item.ecommerce_channel_overrides`: eine `exclude`-Zeile gewinnt über beide Module, eine `include`-Zeile fügt einen Kanal hinzu, den keines der beiden Module produziert hätte. Die Auflösungsreihenfolge ist in [`docs/multi_shop_setup.md`](docs/multi_shop_setup.md) beschrieben.

---

## RAG-Integration (Vektorsuche)

Export der Produktdaten als Vektor-Embeddings nach **Pinecone**, als Grundlage für KI-gestützte Shop-Suchassistenten und Chatbots:

- Automatische und manuelle Bulk-Synchronisation über eine eigene Warteschlange
- Filterbar nach Artikelgruppe und Verkaufsfähigkeit
- Eigenes Audit-Log (`RAG Log`)

**Einrichtung:**
1. `RAG Setting` in ERPNext öffnen
2. Pinecone-API-Schlüssel hinterlegen
3. Synchronisationsfilter und Batch-Größe konfigurieren

---

## AI Description (KI-Produktbeschreibungen)

Automatische Generierung von Produktbeschreibungen, Kurzbeschreibungen, Vorteils-Listen und SEO-Texten über **Google Gemini**:

- Lang- und Kurzbeschreibung, Benefits, SEO-Meta-Beschreibung
- Geplante Stapelverarbeitung (stündlich, konfigurierbares Intervall) für noch nicht bearbeitete Artikel
- Rate-Limit-Behandlung für die Gemini-API
- Eigenes Audit-Log (`AI Description Log`)
- Ergebnisse fließen automatisch in den Shopware-/Medusa-Push ein (Beschreibungsfelder, Zusatzfelder)

**Einrichtung:**
1. `AI Description Setting` in ERPNext öffnen
2. Google-Gemini-API-Schlüssel hinterlegen
3. Stapelverarbeitung und Intervall konfigurieren

---

## Kanalübergreifende Funktionen

- **Ecommerce Item**: zentrale ID-Zuordnungstabelle zwischen ERPNext-Artikeln und Backend-IDs (Shopware/Medusa), inkl. Varianten
- **Ecommerce Channel Branding**: Logo, Absenderadresse, IBAN/BIC, Impressum und Signatur je Verkaufskanal — Druckformate und E-Mails ziehen diese Daten zur Laufzeit
- **Ecommerce Channel Override**: manuelle Sichtbarkeits-Ausnahmen je Artikel und Kanal
- **Kanalbewusste Benachrichtigungen**: E-Mail-Versand (Absenderadresse, CC-Routing, E-Rechnung) richtet sich nach dem Herkunftskanal des Belegs
- **Deutsche Druckformate**: Auftragsbestätigung, Bestellbestätigung, Rechnung, Versandbestätigung — generisch gehalten und je Betrieb anpassbar, gerendert über den in Frappe v16 integrierten Chrome-PDF-Generator
- **E-Rechnung (XRechnung)**: Leitweg-ID wird automatisch in das Standardfeld für die elektronische Adresse gespiegelt

---

## Installation

**Für Shopware 6 / Medusa / RAG / AI Description (dieser Fork):**

```bash
# App aus diesem Fork holen
$ bench get-app ecommerce_integrations https://github.com/DrdotHouse2106/ecommerce_integrations.git --branch feat/multi-channel-integrations

# Auf einer Site installieren
$ bench --site <deine-site> install-app ecommerce_integrations
```

**Für die reine Upstream-Version (Standard-Integrationen von Frappe/ERPNext):**

```bash
# Produktiv
$ bench get-app ecommerce_integrations --branch main

# Entwicklung
$ bench get-app ecommerce_integrations --branch develop

# Auf einer Site installieren
$ bench --site <deine-site> install-app ecommerce_integrations
```

Nach der Installation die Dokumentation der jeweiligen Integration befolgen.

---

## Mitwirken

- Für Shopware 6 / Medusa / RAG / AI Description: Pull Requests gegen den Branch `feat/multi-channel-integrations`
- Für Upstream-Integrationen: den [ERPNext-Beitragsrichtlinien](https://github.com/frappe/erpnext/wiki/Contribution-Guidelines) folgen

## Entwicklungsumgebung

- Entwicklermodus aktivieren
- Für Webhook-Tests: `localtunnel_url` in der `site_config.json` auf eine ngrok-/localtunnel-URL setzen

## Unterstützung

Dieser Fork wird in der Freizeit gepflegt. Wenn er dir weiterhilft, freue ich mich über eine kleine Spende:
[paypal.me/DrdotHouse](https://paypal.me/DrdotHouse)

## Lizenz

GNU GPL v3.0
