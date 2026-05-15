# Repository guide for AI coding agents

This is a public Frappe app — a fork of `frappe/ecommerce_integrations` that adds first-class Shopware 6, Medusa v2, RAG (vector search) and AI description integrations alongside the upstream Shopify, Amazon, Unicommerce and Zenoti modules.

**Target platform: Frappe / ERPNext v16.** Older versions are not supported; the plugin uses v16-only features (in-process Chrome PDF generator, `frappe.types.DF` typing module, current `frappe.tests.IntegrationTestCase`). The dependency pin in `pyproject.toml` (`frappe = ">=16.0.0,<17.0.0"`, `erpnext = ">=16.0.0,<17.0.0"`) is load-bearing — don't widen it without a real reason.

The fork is consumed by other operators. Anything that lands here ships to every install. Treat this as a library, not as one operator's customisation layer.

## Branch model

- `main` — tracks `frappe/ecommerce_integrations:main` 1:1 (upstream's v15 maintenance line). Kept for parity with upstream; **do not target this branch for v16 work**.
- `feat/multi-channel-integrations` — the active v16 integration branch. Carries the Shopware 6 / Medusa / RAG / AI work on top of upstream's v16 line. **Production fork users install from this branch.**
- Day-to-day development happens on short-lived feature branches off `feat/multi-channel-integrations`. Merge back via PR.

When syncing from upstream, the relevant upstream refs are `upstream/version-16` (v16 stable maintenance) and `upstream/develop` (v16 next). Both currently track Frappe v16. `upstream/main` is upstream's v15 line and should not be merged into the integration branch — it would silently downgrade the dependency pin.

Local-only work (operator-specific scripts, debugging snippets, one-off migrations) belongs in your own working tree or a private branch — never on the integration branch.

## The generic-plugin rule

Code, configuration, fixtures, tests, docs, commit messages and branch names must be generic. The plugin must install and run on any Frappe site without manual edits.

What that means concretely:

- **No real-world identifiers in tracked files.** No real company names, brand names, product line names, customer names, employee names, real domains, real ERPNext site names, real bench paths, real item codes, real customer codes, real VAT IDs, real IBANs/BICs, real order numbers.
- **Use placeholders.** `example.com`, `yourshop.com`, `<your-site>`, `sitename`, `ITEM-001`, `CUST-001`, `Test GmbH`, `Demo Kunde`, `Demo Kunde GmbH`, `Musterstraße 1`, `10115 Berlin`, `DE123456789`. Reach for these whenever an example needs a value.
- **Branding is data, not code.** Logos, sender names, sender email accounts, support emails, IBANs, BICs, imprint HTML, signature HTML, privacy policy URLs, terms URLs and shop URLs all live on the `Ecommerce Channel Branding` doctype. Print formats and email templates pull them at render time via `{{ brand.X }}`. Never hardcode.
- **Notification recipients come from the document.** Fixture notifications use `receiver_by_document_field: "contact_email"` and dynamic CC routing. No hardcoded CC/BCC addresses in `fixtures/notification.json`.
- **Generic naming series only.** Existing series are `CUST-.YYYY.-`, `AB-.YYYY.-`, `SO-.#####`. Anything new follows the same pattern. No `<COMPANY>-XXXX-`-style series.
- **Generic test fixtures.** Tests in `*/tests/data/*.json` use placeholder addresses, placeholder VAT IDs and placeholder names. No real customer or order data, even from anonymised exports.

## Module layout

Top-level package is `ecommerce_integrations/`. Each integration lives in its own subpackage and follows the upstream layout:

```
ecommerce_integrations/
  ecommerce_integrations/   # cross-channel doctypes, notifications, branding, print formats
  shopware6/                # Shopware 6 integration (orders, products, inventory, prices)
  medusa/                   # Medusa v2 integration (orders, products, customers, payments)
  catalog_mirror/           # 1:1 IG-tree → backend-category-tree mirror (Shopware + Medusa)
  smart_collections/        # rule-based item groupings; pushes onto channels/categories
  shopify/    amazon/    unicommerce/    zenoti/   # upstream-maintained
  rag/                      # vector search export (Pinecone)
  ai_description/           # AI product description generator (Gemini)
  controllers/              # cross-channel controllers (customer, inventory, scheduling)
  patches/                  # idempotent migration scripts
  fixtures/                 # shipped Notification / Email Template fixtures
  templates/includes/       # shared Jinja partials for emails and print formats
  utils/                    # shared helpers (taxation, naming, …)
```

Cross-channel concerns (channel branding, channel-aware notifications, generic ecommerce custom fields, the integration log doctype) live under `ecommerce_integrations/ecommerce_integrations/`. Anything channel-specific lives under `<channel>/`.

Key cross-channel doctypes worth knowing about:

- `ecommerce_integrations/doctype/ecommerce_item/` — canonical ERP→backend
  external-ID mapping table (`integration='shopware'|'medusa'|...`).
  Reads/writes for Medusa IDs go here, not `Item.medusa_product_id`
  (that custom field is dropped by `drop_medusa_item_custom_fields`).
- `ecommerce_integrations/doctype/ecommerce_channel_override/` — child
  table referenced by `Item.ecommerce_channel_overrides`. One row per
  (backend, sales_channel) with `mode=include`/`exclude` — fed into the
  unified resolver in `catalog_mirror/resolver.py`.
- `Item Group.shopware_category_id` / `medusa_category_id` /
  `catalog_mirror_skip` — custom fields added by
  `patches/setup_catalog_mirror`. The two `*_category_id` columns are
  the Catalog Mirror's persistent IG→backend-category lookup; the skip
  flag opts an IG (and its subtree) out of every mirror walk.

The Shopware 6 module is the architectural reference — the Medusa module mirrors its patterns (class-based sync objects, decorator-driven session management, bulk queue, doc event hooks).

## Patches

`patches.txt` lists patches in execution order. Every patch in `ecommerce_integrations/patches/` must be:

- **Idempotent.** Calling `execute()` twice produces the same final state.
- **No-op safe.** Guard with `if not frappe.db.exists("DocType", "<X>"): return` so the patch is a no-op when the integration isn't installed on this site.
- **Generic.** Reference only doctypes/fields/print formats this plugin ships. A patch that disables a notification only the operator's site has does not belong here.
- **Property-Setter-driven for naming series.** Adding a series option uses a Property Setter so the change survives ERPNext upgrades.

If a patch only applies to one operator's installation, keep it in your own private bench, not in `patches/`.

Notable patches in the integration branch (full intent lives in each file's module docstring):

- `migrate_medusa_ids_to_ecommerce_item` — backfills `tabEcommerce Item` rows for every Medusa-tagged Item, sourcing IDs from the legacy `Item.medusa_product_id` / `Item.medusa_variant_id` custom columns. Verifies row counts before declaring success.
- `drop_medusa_item_custom_fields` — removes the legacy `Item.medusa_product_id` / `Item.medusa_variant_id` custom fields once the backfill above has produced an `Ecommerce Item` row for every source row. Self-guarding: skips if rows are still missing.
- `setup_catalog_mirror` — installs the `Item Group` custom fields the Catalog Mirror depends on: `shopware_category_id`, `medusa_category_id`, `catalog_mirror_skip`.
- `add_item_ecommerce_channel_overrides` — installs the `Item.ecommerce_channel_overrides` table custom field pointing at the `Ecommerce Channel Override` child doctype.
- `migrate_shopware_umbrellas_to_catalog_mirror` — detects the legacy "single-rule Item-Group Smart Collection per sales channel" pattern, creates the equivalent Catalog Mirror doc and disables the SC's Shopware target (non-destructively — a breadcrumb on `last_error` records the migration so rollback is one click).

## Hooks

`hooks.py` registers cross-channel behaviour. Key contracts:

- `doc_events` for `Item`, `Item Group`, `Sales Order`, `Sales Invoice`, `Delivery Note` fan out to each integration's `bulk_sync.queue_*` entry point. Adding a new channel means adding an entry, not touching existing channels.
- `override_doctype_class` swaps in `ChannelAwareNotification` for the standard `Notification` so per-channel branding/email-account/E-invoice routing works.
- `jinja.methods` exposes `get_branding`, `build_greeting_context`, `render_branding_text`, `get_invoice_payment_context`, `get_default_bank_info`. Anything new called from a notification or print format must be added here — Frappe's Jinja sandbox blocks unregistered methods.
- `fixtures` syncs all `Notification`s tagged with `module = "ecommerce_integrations"` on `bench migrate`.
- `required_apps` declares `frappe/erpnext`. Don't drop this.

## Working with upstream

The integration branch tracks upstream's v16 line. `upstream/version-16` is stable v16 maintenance; `upstream/develop` is v16 next. Use whichever matches the cadence of changes you want to pick up.

- Merge with `git merge upstream/version-16 -X theirs` (or `upstream/develop`). Conflicts in upstream-owned files (Shopify, Amazon, Unicommerce, Zenoti, controllers, `hooks.py` upstream sections) resolve to the upstream version because we don't intentionally fork those modules — only extend `hooks.py` to add our channels.
- **Do not merge `upstream/main`** — it's the v15 maintenance line. Pulling it in flips the `pyproject.toml` dependency pin back to v15 and breaks the install on a v16 site. If a `-X theirs` merge ever puts `frappe = ">=15.0.0,<16.0.0"` back into `pyproject.toml`, fix it before pushing.
- Don't run formatters across upstream files. Style drift is the main source of merge conflicts.
- New cross-channel logic should land in `controllers/` or `ecommerce_integrations/ecommerce_integrations/`, not be sprinkled into `shopify/` or `amazon/`.

## Code conventions

- Python ≥ 3.10, Frappe / ERPNext v16 idioms. Use `X | None` not `Optional[X]`. Use `frappe.types.DF` for type annotations on doctype controllers. Use `frappe.tests.IntegrationTestCase` for tests.
- Match existing style of the file you're editing. The integration modules (`shopware6/`, `medusa/`, `rag/`, `ai_description/`) use 4-space indent and PEP 8 line lengths. Upstream files use tabs — leave them tabbed.
- Class-based integration objects (`ShopwareProduct`, `MedusaCustomer`, …) follow the upstream `ShopifyProduct` pattern: constructor takes the integration ID, `is_synced()` / `get_erpnext_item()` / `sync_*` methods on the instance.
- Bulk syncs go through a queue. Every doc event handler calls `<channel>.bulk_sync.queue_*_for_sync(doc, …)`, which dedups and enqueues. Don't call sync methods directly from hooks — that blocks the request.
- Errors during sync are logged to `Ecommerce Integration Log`, never raised back to the user.
- Long-running scheduled jobs (full reconciliation, batch product export) check a generation counter and abort cleanly if a newer run has been queued.
- Secrets (API keys, webhook secrets) are stored as `Password` fields and read via `get_password()`. Never log secret values.

## Tests

- Tests live next to the code they test (`<module>/tests/test_*.py`).
- Test data is in `<module>/tests/data/*.json` and uses generic placeholders only.
- Integration tests inherit from `frappe.tests.IntegrationTestCase`. Don't mock the database for tests that exercise the sync pipeline — mocked DB tests have repeatedly let migration bugs reach production.
- Only commit a test if it passes in this checkout. Skipped or `xfail`-marked tests need a tracking note in the body explaining why.

## Documentation

- `README.md` is the public entry point. Keep installation instructions, supported integrations, contribution flow.
- `docs/` holds longer-form guides (`docs/multi_shop_setup.md` is the multi-channel setup walkthrough).
- Inline docstrings explain the *why* — the function name and signature already explain the *what*. Don't restate the code.
- Don't reference internal incidents, customer reports or ticket IDs in code comments or commit messages.

## Commit messages

- Conventional Commits with a scope: `feat(medusa):`, `fix(shopware6):`, `chore(rag):`, `docs:`, `refactor(ecommerce_integrations):`.
- Subject line ≤ 72 chars, imperative, no trailing period.
- Body explains the *why*. The diff already shows the *what*.
- No incident references, no customer names, no internal ticket numbers.

## Things this plugin deliberately does

- **Ships German print formats.** `Auftragsbestätigung`, `Bestellbestätigung`, `Rechnung`, `Versandbestätigung` — these are the four standard German order-flow document names. They render generically; customers in other locales can disable them or override.
- **Defaults email/print copy to German.** Field defaults like "Sehr geehrte Damen und Herren" come pre-filled on `Ecommerce Channel Branding` because the primary user base is German Shopware 6 / Medusa shops. Defaults are user-editable.
- **Uses Chrome PDF generator for ecommerce print formats.** wkhtmltopdf fails inside containers that can't resolve their own hostname; Frappe v16 ships an in-process Chrome generator. The patch `set_chrome_pdf_generator` switches the four shipped formats to Chrome on `bench migrate`. This is one of the v16-only features the plugin relies on.
- **Routes invoices through `Ecommerce Sales Invoice` notification.** Fires only when `doc.ecommerce_source` is set, so non-ecommerce invoices aren't affected.
- **Ships a per-item Channel Override mechanism** (`Item.ecommerce_channel_overrides` table, child doctype `Ecommerce Channel Override`) that wins over Catalog Mirror and Smart Collections in the visibility resolver. `mode=exclude` is a hard veto on a channel for that item; `mode=include` injects a channel neither layer produced. Backend-scoped per row.
- **Stores ERP→backend external IDs in `tabEcommerce Item`** (canonical, `integration='shopware'|'medusa'`) **AND on `Item Group.shopware_category_id` / `medusa_category_id`** (for Catalog Mirror's IG→category tree mapping). The two storage locations are not duplicates: `tabEcommerce Item` maps Items, the IG custom fields map category nodes.

## Things this plugin deliberately does not do

- **No hardcoded customer/brand/category lists.** Anything that reads "if item.brand in ['BrandA', 'BrandB']" is a bug. Make it configurable on a Setting doctype or derive it from the data.
- **No bench/site-specific bash scripts.** No `bench --site <real-site> execute …` examples in tracked code. Use `<your-site>` placeholder.
- **No one-off migration scripts at module roots.** Scripts named `fix_*`, `check_*`, `debug_*`, `analyze_*`, `copy_*`, `swap_*` belong in your private working tree. They are not tracked.
- **No upstream-file reformatting.** Running `ruff format` across the whole tree creates 800+ lines of churn against `frappe/ecommerce_integrations` and breaks every future merge.
