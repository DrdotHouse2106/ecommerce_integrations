# Sicherheitsaudit 2026-10-07 (Branch `feat/multi-channel-integrations` @ 677966b)

Issues sind in diesem Fork deaktiviert, daher liegen die Befunde hier als Datei. Jeder Punkt ist eine
abhakbare Aufgabe. Pfade relativ zu `ecommerce_integrations/ecommerce_integrations/`.
Status **bestätigt** = Codepfad vollständig nachvollzogen, **plausibel** = abhängig von Umgebung/Rollen.

## Hoch

### H1 – Beliebiges Datei-Lesen + SSRF über Item-Group-Kategoriebild
**Status:** Code-Kette bestätigt, Ausnutzbarkeit plausibel
**Stellen:** `shopware6/export/category_handler.py:181-228` (`get_image_content_and_filename`); Aufrufer
`upload_category_media` (`:262`), `sync_category_hierarchy` (`:431-433`, `:1439-1444`),
`force_resync_category_image` (`:912-917`); Quelle `get_item_group_data()` `:55-56`.

`Item Group.image` / `category_image` wird roh gelesen: `http`-Werte gehen ungeprüft an `requests.get`,
`/files/…` wird ohne Normalisierung an `get_files_path()` gehängt, jeder andere String wird als absoluter
Pfad mit `open()` gelesen. Das Ergebnis wird als öffentliches Shopware-Medium hochgeladen.

**Angriff:** Nutzer mit Schreibrecht auf Item Group (kein System Manager nötig) setzt
`category_image = "/home/frappe/frappe-bench/sites/<site>/site_config.json"` oder
`"/files/../../site_config.json"` oder `http://169.254.169.254/…`. Beim nächsten Kategorie-Sync landet die
Datei im Shop und ist über die Storefront-CDN abrufbar.

- [ ] Pfad ausschließlich über das `File`-DocType auflösen (`frappe.get_doc("File", {"file_url": …}).get_content()`), kein File-Dokument → `None`
- [ ] `else`-Zweig (absoluter Pfad) entfernen
- [ ] `http(s)`-Zweig streichen oder auf Host-Allowlist (nur HTTPS, keine privaten IP-Bereiche, kein Redirect-Follow)
- [ ] Defense in depth: `os.path.realpath` + Prefix-Check gegen `get_files_path()`

## Mittel

### M1 – Medusa-Webhook übernimmt eingebettete Bestell-/Kundendaten ohne Gegenprüfung
**Status:** bestätigt
**Stellen:** `medusa/webhook_handler.py:31-150`, `medusa/order/order_sync.py:36-66`
(`so.insert(ignore_permissions=True)`, `flags.ignore_mandatory=True`), `medusa/order/order_mapper.py:120-145`
(`rate`/`discount_amount` aus Payload), `medusa/customer.py:41-65, 95-170, 316-349`.

`sync_order()` ruft bei eingebettetem `order`-Objekt nie `fetch_order()` auf. Vorbedingung:
`allow_unsigned_webhooks=1` oder geleaktes Secret. Dann kann jeder Unauthentifizierte submittierbare Sales
Orders mit frei gewählten Preisen, Kunden und Adressen anlegen, bestehende Kunden umbenennen
(`customer.updated`) oder deaktivieren (`customer.deleted`), inkl. Versand der Bestellbestätigung.

- [ ] Order und Customer immer per Admin-API nachladen (`MedusaOrder.fetch_order()` existiert), eingebettete Daten nur als Cache bei ID + `updated_at`-Match
- [ ] `allow_unsigned_webhooks` nur wirksam, wenn `frappe.conf.developer_mode` gesetzt ist
- [ ] `sync_customer_by_id`: bei Payload-Form `fetch_from_medusa()` erzwingen

### M2 – Shopware-Zahlungswebhook bucht Rechnung + Zahlung allein aufgrund des Payload-Status
**Status:** bestätigt
**Stellen:** `shopware6/payment.py:140-290` (`handle_transaction_state_change`, `:471-472`
`payment_entry.insert(ignore_permissions=True); submit()`), `shopware6/order/order_sync.py:646-709`
(`update_order_custom_fields`, `db.set_value` auf Sales Order und Customer ohne Validierung).

`state == "paid"` aus dem Payload → submittierte Sales Invoice + Payment Entry.
`verify_payment_status_from_shopware` (`order/payment_handler.py:145ff`) existiert, wird hier nicht genutzt.

- [ ] Transaktionsstatus vor dem Buchen bei Shopware nachladen (`search/order-transaction`)
- [ ] `webhook_custom_fields` nur als Fallback, Schreiben über `doc.save()` statt `db.set_value`

### M3 – Unauthentifiziertes Log-Flooding über den Shopware-Webhook
**Status:** bestätigt
**Stellen:** `shopware6/connection.py:589-679`, `ecommerce_integration_log.py:56-76`.

Jeder abgelehnte Request (falsche Signatur, ungültiges JSON) läuft in `except Exception → persist=True`
→ committeter `Ecommerce Integration Log` (120 Tage). Kein `methods=["POST"]`, kein Body-Limit.

- [ ] `AuthenticationError`/`ValidationError` nicht persistieren (nur `frappe.logger().warning`)
- [ ] `@frappe.whitelist(allow_guest=True, methods=["POST"])`
- [ ] Body-Größe vor dem HMAC prüfen (z. B. > 1 MB → 413), Frappe-Rate-Limit dokumentieren

### M4 – Medusa: Adress-Wiederverwendung über Kundengrenzen hinweg
**Status:** Logik bestätigt, Sichtbarkeit auf Belegen plausibel
**Stelle:** `medusa/order/order_mapper.py:236-276` (`_get_or_create_address`).

Lookup nur über `address_line1`/`city`/`pincode` ohne Kundenfilter. Ein Shop-Kunde, der die Adresse eines
anderen eingibt, bekommt dessen `Address` samt `address_title`, `phone`, `email_id` an seinen Customer
gelinkt und auf seine Belege gedruckt. Leere Felder matchen alle Adressen mit leerem `address_line1`.

- [ ] Lookup auf die Dynamic Links des jeweiligen Kunden einschränken (wie `shopware6/customer/sync.py:178-188`)
- [ ] `country` + Name vergleichen, bei leeren Pflichtfeldern nicht matchen

### M5 – KI-HTML landet ungefiltert per `db_set` im Item und wird in den Shop gepusht
**Status:** Bypass bestätigt, Exploit-Kette plausibel
**Stellen:** `ai_description/gemini.py:465-495` (`db_set("ai_long_description", …)`), `:362-377` und
`:757-764` (`item.description` im Prompt), `:843-852` (Teilstring-Match beim Zuordnen),
`product_sync/engine/canonical.py:896-899` (`_render_description` ohne Prüfung von `ai_description_reviewed`).

`db_set` umgeht Frappes HTML-Sanitizer. Shop-Beschreibungen fließen in den Prompt (Prompt Injection), die
Antwort kann `<script>` enthalten und wird in den Shop-Storefront gepusht.

- [ ] `frappe.utils.sanitize_html(...)` (oder strikte Allowlist) vor jedem `db_set`
- [ ] In `_render_description` nur pushen, wenn `ai_description_reviewed == 1`
- [ ] KI-`item_code` nur bei exaktem Match akzeptieren
- [ ] `{description}` im Prompt als untrusted Data kennzeichnen und kürzen

### M6 – Checkout-Felder roh auf Customer, Templates ohne Escaping
**Status:** Bypass bestätigt, Exploit plausibel
**Stellen:** `ecommerce_integrations/checkout_utils.py:100` (`frappe.db.set_value("Customer", …)` mit
kundengesteuerten Shop-Checkout-Werten, nur `meta.has_field`-Check in `:68`);
`templates/includes/email_branding.html:66, 69, 86, 112`, `print_branding.html:241-260`,
Print-Format `versandbestaetigung.json` Zeile 65 (`{{ doc.terms }}`).

Frappes `render_template` hat kein Autoescape. Operator mappt ein Checkout-Feld z. B. auf
`customer_details`; Shop-Kunde trägt `<img src=x onerror=…>` ein → roh in `tabCustomer` → Desk/PDF/E-Mail.
`custom_tracking_url` ohne Schema-Check in `href` (`javascript:` möglich).

- [ ] Werte in `checkout_utils.py` mit `sanitize_html`/`strip_html` bereinigen und auf Feldlänge kürzen
- [ ] `target_field` auf Allowlist beschränken (keine `name`/`owner`/Link-Felder)
- [ ] `| e` in Templates für doc-/item-/address-Felder, `custom_tracking_url` nur bei `http(s)://` verlinken

## Niedrig

- [ ] N1 Webhooks: kein `methods=["POST"]`, kein Zeitstempel-Replay-Schutz; Shopware-Dedup hängt am angreiferkontrollierten Header `sw-context-message-id` (`event_queue.py:101-122`). Zeitstempel in den HMAC, > 5 min ablehnen.
- [ ] N2 `utils/naming_series.py:4-10` `get_series()` ohne Berechtigungsprüfung.
- [ ] N3 Health-Banner meldet grün bei `allow_unsigned_webhooks=1` (`shopware6/api/setup.py:92-98`, `medusa/api/setup.py:86-91`). Als Warnung anzeigen.
- [ ] N4 `fire_test_webhook` nutzt `get_url()` aus dem Host-Header (`shopware6/api/setup.py:341-399`, `medusa/api/setup.py:313-367`). `host_name` aus `site_config` verwenden.
- [ ] N5 Item-Write reicht für submittierte Stock Entries (`stock_importer.py:155-156, 325-370`) und Preis-Push aus beliebiger Preisliste (`price_handler.py:454-470`); `property_importer.py:379-420` akzeptiert beliebige `shopware_id`. `has_permission("Stock Entry","create")`, `price_list` validieren.
- [ ] N6 `dry_run`-Parameter als String truthy (`stock_importer.py:233, 325`, `property_importer.py:423`). `frappe.utils.sbool`.
- [ ] N7 KI-Generierung mit bloßem Item-Schreibrecht, Rate-Limit nicht atomar (`ai_description/api.py:25-83, 197-218, 304-360`). Eigene Rolle, Redis `INCR`+`EXPIRE`, Tagesbudget.
- [ ] N8 Branding-Helfer unnötig whitelisted: `get_branding`, `get_default_bank_info` (IBAN/BIC an jeden eingeloggten User), `build_greeting_context` (Kontakt-IDOR), `get_available_channels` (`ecommerce_channel_branding.py:72, 108, 281, 316`). `@frappe.whitelist()` entfernen, `jinja.methods` reicht.
- [ ] N9 `render_pdf_preview`/`find_sample_doc` akzeptieren beliebigen `doc_type` (`ecommerce_channel_branding.py:349-386, 501-517`); Preview per `document.write` im ERPNext-Origin (`.js:44-55`). Allowlist + sandboxed `<iframe srcdoc>`.
- [ ] N10 SSTI by design: Branding-Texte und Product-Sync-/Catalog-Mirror-Templates laufen durch `frappe.render_template` mit `frappe`-Namespace (`ecommerce_channel_branding.py:304-313`, `canonical.py:860, 892, 915, 1530`, `catalog_mirror/differ.py:512-540`). Nur System Manager editierbar; dokumentieren, keine weiteren Rollen vergeben, optional reduzierte Environment.
- [ ] N11 Fixture „Ecommerce Delivery Note" referenziert nicht mitgeliefertes Email Template (`fixtures/notification.json:20`); `boot.py:8-11` importiert nicht existierendes Modul `einvoice_patch`; `_retry_job` (`ecommerce_integration_log.py:95-113`) enqueued jede App-Funktion mit beliebigem Payload (SM-only).
- [ ] N12 Generic-Plugin-Verstöße: `jattr_`-Präfix in `rag/embedding.py:238`, `rag/product_export.py:376`; hartkodierte Zahlungsarten-Liste `ecommerce_channel_branding.py:212-216, 270`; Pinecone-Host frei editierbar (`rag/connection.py:38-40`, API-Key geht an diesen Host); `include_custom_fields` exportiert beliebige Item-Felder; 120 Tage Webhook-PII im Integration Log.

## Geprüft und sauber
HMAC beider Webhooks (roher Body, `compare_digest`, fail-closed per Default). Alle 102 `frappe.db.sql`-Aufrufe
parametrisiert. Kein `eval`/`exec`/`pickle`/`verify=False`. Alle Admin-Endpunkte mit `only_for("System Manager")`
oder `require_*_admin`. Secrets ausschließlich als `Password`-Felder, `boot.py` gibt nichts an den Browser.
JS escaped Serverdaten durchgängig. Shopify-Fork-Diff ohne sicherheitsrelevante Änderung. Upstream-Module
(Shopify, Amazon, Unicommerce, Zenoti) nur im Fork-Diff geprüft.
