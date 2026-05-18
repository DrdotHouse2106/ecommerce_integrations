# Product Sync — Operator's Guide

Product Sync is the **single source of truth** for pushing ERPNext
items to Shopware 6 and Medusa v2. One doctype (`Ecommerce Product
Sync`) owns scope selection, preview, sandbox testing, and apply.
Settings forms only host operational tools (Test Connection, Clear
Cache); every actual push lives on a Product Sync doc.

## Why Product Sync exists

Older forks pushed items in a tangle of per-Setting buttons:
"Complete Sync", "Rebuild Prices", "Force Image Sync", and so on.
None of them shared scope, none had per-item hashing, none produced
audit rows. Product Sync collapses all of that into:

- **One doctype per scope** — a Sync is the answer to "which items
  go to which storefronts, with what field toggles, on what schedule".
- **Hash-delta detection** — every Item's canonical payload is SHA-256
  hashed; the differ only re-pushes items whose hash changed. A 33k-
  item nightly sync touches the backend for ~tens of items, not all
  thirty thousand.
- **Three preview modes** — Schnell (hash only, ~30 s), Detail
  (per-field diffs against the live backend, ~2 min), Vollständig
  (+ orphan walk).
- **Audit-grade Sync Runs** — every apply produces a `Ecommerce Sync
  Run` row with bucket counts, per-item applied diffs, and an
  optional pre-apply snapshot for rollback.

## Setup walkthrough — from zero to first production sync

### 1. Shopware Setting (one-time)

Open **Shopware Setting** in the desk. Fill these *before* creating
any Product Sync:

| Section | Field | What to put |
|---|---|---|
| Connection | `Shop URL` | Public-facing Shopware URL (e.g. `https://shop.example.com`). With trailing slash forbidden. |
| Connection | `Image public base URL` (new) | Leave empty when Shopware can reach this ERPNext install's URL directly. Set to a CDN / reverse-proxy URL when ERPNext is behind a host Shopware cannot resolve (e.g. `https://files.shop.example.com`). |
| Storefronts | Click **"Refresh Sales Channels"** | Populates the child table with sales-channel UUIDs. Product Syncs reference these. |
| Customers | `Default Customer` | Fallback for guest orders. Without this, inbound Sales Orders fail. |
| Customers | `Customer Group` | The group new customers from Shopware land in. |
| Firma & Buchhaltung | `Company`, `Default Warehouse`, naming series | Standard ERPNext plumbing. |
| Preise | `Default Price List` | The source of truth for outbound product prices. |
| Versandkosten | `Shipping Item`, `Capture Shipping As Line` | `VERSAND` or similar service item; check the line-capture flag. |
| Versandkosten | `Discount Item` | `RABATT` service item for inbound promo discounts. |
| Maintenance | Click **"Test Connection"** | Should report "OK — N sales channels". |

### 2. Catalog Mirror (one-time per backend tree)

Product Sync **does not** sync categories. Instead it reads the
backend-category UUID from `Item Group.shopware_category_id` (or
`.medusa_category_id`). Those columns are populated by **Catalog
Mirror**.

Open the **Catalog Mirror** for your backend ("Produkte" or
similar). Two scenarios:

- **First-time setup, Shopware already has categories.** Click
  **"Auto-Adopt by Path"** — the mirror matches existing Shopware
  categories by their breadcrumb to ERPNext Item Groups and persists
  the UUIDs. Tested on real installs: 231 IGs adopted in seconds,
  zero ambiguous.
- **Fresh Shopware, no categories yet.** Set the mirror `is_active=1`
  and click **"Apply Live"**. Shopware categories are created to
  mirror the ERP tree.

Product Sync's pre-flight check warns ``category_bridge_missing``
when item groups in scope lack a backend-category mapping. Don't
ignore it.

### 3. Price List tax flag (one-time per Price List)

A new custom field `Price List.custom_price_includes_tax` (installed
by patch on migrate) decides whether the prices in a given list are
gross (incl. VAT) or net (excl.). Real installs typically have:

| Price List | Tax mode | Häkchen |
|---|---|---|
| Standard B2C (e.g. "Standard-Verkauf") | Gross | ☑ AN |
| Standard B2B (e.g. "Standard-Vertrieb") | Net | ☐ AUS |
| MSRP / Streichpreis | usually net | ☐ AUS |

Why this matters: the canonical pricing builder normalises everything
to **gross** before hashing. If the Price List flag is wrong, the
sync pushes the *wrong* gross price to Shopware. Mistake mode for a
B2C shop would be to leave a gross list flagged net — the sync would
multiply by 1.19 and customers would see a 19 % markup.

### 4. Create the Product Sync

Click **"Ecommerce Product Sync" → New** in the desk (or hit the
"Product Sync öffnen" button on Shopware Setting).

| Section | Field | Typical setting |
|---|---|---|
| Allgemein | `Title` | Internal label, e.g. "Hauptshop — alle Produkte". |
| Allgemein | `Backend` | `Shopware` (or `Medusa`). |
| Allgemein | `is_active` | **Off** for the first test runs. Turn on only after a successful sandbox subset run. |
| Allgemein | `Priority` | Higher number wins on conflicts (an Item matching multiple Syncs). Leave 10 unless you have multiple Syncs. |
| Allgemein | `Target Sales Channels` | One row per storefront UUID. Mark exactly one as `is_primary`. |
| Auswahl der Artikel | `Scope Mode` | Most common: `Catalog Mirror` (sync everything the mirror covers). Alternatives: `Item Group Subtree`, `Smart Collection`, `Custom Filter`, `All`. |
| Auswahl der Artikel | `Linked Catalog Mirror` | The mirror doc whose IG subtree drives scope. |
| Auswahl der Artikel | `include_descendants`, `include_variants`, `include_disabled` | Operator decisions. |
| Was synchronisiert wird | `sync_basic_fields`, `sync_pricing`, `sync_inventory`, `sync_images`, `sync_properties`, `sync_seo_fields`, `sync_taxes` | Per-section toggles. Turn `sync_basic_fields` off if Shopware has higher-quality marketing copy you don't want to overwrite. |
| Inhalt | `Description Source` | `item_description` (raw), `ai_generated` (uses `Item.ai_long_description`), or `custom_template` (Jinja). |
| Inhalt | `Auto-generate AI descriptions` | If using `ai_generated`: when on, the apply step calls Gemini for items missing AI text before pushing. Capped by `max_ai_pre_generate`. |
| SEO-Fallback | `Meta-Title-Vorlage`, `Meta-Description-Vorlage`, `Slug-Vorlage` | Jinja templates rendered when the corresponding Item field is empty. Variables: `{{ item }}`, `{{ sync }}`, `{{ brand }}`. |
| Inhalt | `Price Strategy` | `item_standard_rate` (uses `Item.standard_rate`), `channel_price_list` (per-channel price lists), `custom_markup` (% over standard). |
| Inhalt | `Tax Template` | Item Tax Template the resolver uses when neither `Item.taxes` nor a Setting default is set. Pre-flight warns if empty. |
| Lebenszyklus | `Orphan Policy` | `keep` (default — never auto-delete), `deactivate`, `delete`, `report`. |
| Lebenszyklus | `Update Policy` | `only_if_changed` (hash-delta) is the default and what you want. |
| Lebenszyklus | `Conflict Policy` | `manual_review` (default). |
| Zeitplan | `Cron Preset` | `manual` until you trust the sync. |

### 5. Run the Pre-Flight Check

On the Sync form: click **"Test Run → Pre-Flight Check"**. It surfaces:

- **block** findings: missing backend, empty scope, missing required
  fields. The apply button is locked until these clear.
- **warn** findings: missing tax template, inactive sync, missing
  category bridge. Apply is allowed but the operator should review.
- **ok** findings: confirmations like "scope contains 33k+ items".

The `category_bridge_missing` warning is the most common — fix it by
running Catalog Mirror's "Auto-Adopt by Path" or Apply Live first.

### 6. Run a Schnellvorschau

Click the **"Vorschau"** button (Ctrl+Enter shortcut). A progress
dialog opens with a percentage bar — for big catalogues this takes
30–60 s.

The result dialog shows four buckets:

- **Create**: items not yet on the backend.
- **Update**: items whose stored hash ≠ proposed hash.
- **NoOp**: items whose hash matches — apply skips these entirely.
- **Mapping Drift**: items with a stale external_id (backend product
  was deleted out-of-band).

Schnellvorschau does **not** call the backend for per-item field
diffs — it only counts buckets via the hash. That's the right
preview mode for "do I have lots of changes to review?".

### 7. Run a Detailvergleich

For each item in the **Update** bucket, click → **"Detailvergleich
(mit Backend)…"**. The differ now fetches each item's live state
from Shopware and shows per-field diffs:

```
basic.description  modified
  live:     <p>Der Brand_G Werkstattschrank Basis PLUS (1950 × 930…
  proposed: <p>Stahl-Flügeltürenschrank Basis PLUS, Korpus…
pricing.base_price modified  (risk: price_jump)
  live:     485.52
  proposed: 408.00
```

Items flagged `risk_flag=price_jump` (> 20 % movement) are the ones
to review most carefully — they're typically the result of a data
entry error in ERPNext or a wrong Price List flag.

### 8. Sandbox subset test

Before turning the Sync loose on production:

1. Mark one Sales Channel as `is_sandbox=1` in Shopware Setting.
2. On the Sync, set `default_sandbox_channel` to that channel.
3. Click **"Test Run → Subset Test…"** and supply 5–10 SKUs.
4. The apply path runs ONLY against the sandbox channel.
5. Verify in Shopware that the test products look correct.

### 9. Apply Live

Only after the sandbox subset confirms the diff plan looks right.
The "Apply Live" button asks for a double-confirm. Behind the
scenes:

- Apply runs on the `long` worker queue.
- Items are batched in groups of 25 against Shopware's
  `_action/sync` endpoint (synQup's recommended chunk size for
  complex entities).
- Each batch is one HTTP roundtrip; 33,961 items become ~1,359
  round trips instead of 33,961.
- After each batch, per-item errors are surfaced to the audit row.
- The Sync Run live-progresses (you can leave the form and watch
  via "Test Run → Test-Historie…").

## Three image-sync gotchas

1. **Shopware needs a URL it can actually fetch.** ERPNext stores
   files at `/file/<hash>/<name>.jpg`. The default base URL is
   whatever `frappe.utils.get_url()` returns — internal hosts like
   `http://erp.internal:8080` are not reachable from cloud-hosted
   Shopware. Set `Shopware Setting → Image public base URL` to a
   CDN / reverse-proxy URL that resolves externally.

2. **Two-step upload**, not single-shot. Shopware's sync API with
   `media: [{media: {url}}]` only creates *empty* Media records.
   The adapter handles this correctly by calling
   `POST /_action/media/{id}/upload?fileName=&extension=` per image
   after the product upsert. The orchestrator triggers this when
   `sync_images=1`.

3. **Image-diff compares filenames, not URLs.** ERPNext URLs
   (`/file/<hash>/name.jpg`) and Shopware CDN URLs
   (`cdn.example.com/.../name.jpg`) will always differ. The differ
   anchors on the basename instead. Without this the image diff
   would always flag every product as "changed" forever.

## Pull Sync — incoming from Shopware

`Ecommerce Pull Sync` is the inverse of Product Sync: it pulls
orders, customers, and (optionally) inventory **from** the backend
**into** ERPNext on a cron schedule.

| Field | What to put |
|---|---|
| Backend | `Shopware` / `Medusa` |
| Pull Orders / Customers / Inventory | Toggle each entity type |
| Cron Preset | `every_hour` is a sane default |
| Lookback hours | First run uses this when there's no watermark. Default 7 days. |
| Channels | Empty = all storefronts; populate to restrict |

Pull Sync writes a `Ecommerce Sync Run` row with `mode='pull'` per
run. Subsequent runs use `last_<entity>_pulled_at` watermarks so
only deltas come through.

The orchestrator deduplicates against the Pull Sync's child table
(orders already imported are skipped).

## Webhooks

Shopware and Medusa both push events (new order, payment captured,
etc.) to ERPNext via webhook. The handlers live at:

- `POST /api/method/ecommerce_integrations.shopware6.webhook.handler.handle_webhook_event`
- `POST /api/method/ecommerce_integrations.medusa.webhook.<event>` 
  (per-event functions; see the module for the live list)

Signature verification uses `Shopware Setting → Webhook Secret` (and
the Medusa equivalent). Both **must** be set in production — pre-
flight on the setup wizard fails activation if missing.

The same logic powers webhook *and* Pull Sync (`shopware6.order.
order_sync.create_sales_order` for both), so a missed webhook is
caught by the next cron run.

## Failure modes worth knowing about

| Symptom | Cause | Fix |
|---|---|---|
| Preview shows `33,961 updates` with `categories` diff for ~all items | Catalog Mirror never ran — `Item Group.shopware_category_id` is empty | Run Catalog Mirror → Auto-Adopt by Path or Apply Live |
| Every item shows an `images` diff every preview | URL comparison was URL-vs-URL; ERP and Shopware always differ | (Fixed) — image diff now compares by filename. If you still see this, check the file basenames |
| Preview shows `image_count: 7 → 0` after apply | `image_public_base_url` not reachable from Shopware | Set a public-accessible URL or disable `sync_images` |
| Preview shows `pricing.base_price` drop of ~19 % for many items | Tax-template wrong or price list flag wrong | Verify each Item Tax Template's rate; verify each Price List's `custom_price_includes_tax` |
| Apply Live status stuck on `running` for hours | Worker died mid-apply, lock not released | Manually clear: `frappe.db.set_value("Ecommerce Product Sync", "<name>", "sync_status", "ok")` |
| Webhook returns 401 from Shopware | Webhook secret mismatch | Re-paste the secret in both Shopware webhook config and Shopware Setting → Webhook Secret |
| DETAIL preview crashes with `2006 'Server has gone away'` | (Fixed) MySQL idle during long backend fetch | Now handled via in-fetch keepalive + gzip-compressed plan storage |

## Performance characteristics on a 33k-item catalogue

Measured on the dev install, hardware varies:

| Operation | Wall time | Notes |
|---|---|---|
| Schnellvorschau | ~36 s | Pure hash delta, no backend roundtrip |
| Detailvergleich (DETAIL) | ~190 s | + per-item drift fetch + per-field diffs |
| Vollständige Vorschau | + 1–3 min | Adds orphan walk of full target channel inventory |
| Apply Live (full catalogue) | ~5–10 min | Batched at 25 per Shopware `/_action/sync` call |
| Bulk SKU adopt | 112 s | One-time setup: links existing Shopware products by SKU |
| Catalog Mirror Auto-Adopt by Path | < 5 s | Per-path matching, single API call |

## Architecture in one paragraph

`product_sync/walker.py` enumerates Items via the Sync's scope.
`engine/bulk_context.py` bulk-loads Item snapshots, prices, and
mappings in five queries (instead of 4 × N). `engine/canonical.py`
builds the canonical payload + SHA-256 hash per Item — toggle-aware
so disabling a section doesn't break parity. `engine/adapters/*.py`
talk HTTP to the backend (Shopware uses `_action/sync` batched at 25,
Medusa falls back to per-item upsert). `differ.py` classifies each
Item into create / update / noop / drift; for drift candidates it
fetches the live state from the adapter in 500-ID `equalsAny`
batches. `tasks.py` is the orchestrator: it owns the apply loop,
batch boundaries, cancel-flag checks, snapshot persistence, and the
final Sync Run audit row. `api.py` is the whitelisted surface (the
JS form calls these for preview, preflight, apply, adopt, history).
