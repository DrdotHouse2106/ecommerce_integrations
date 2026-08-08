<div align="center">
    <img src="https://frappecloud.com/files/ERPNext%20-%20Ecommerce%20Integrations.png" height="128">
    <h2>Ecommerce Integrations for ERPNext</h2>

[![CI](https://github.com/frappe/ecommerce_integrations/actions/workflows/ci.yml/badge.svg)](https://github.com/frappe/ecommerce_integrations/actions/workflows/ci.yml)

**[Deutsche Version / German version](README.md)**

</div>

> **Note:** This is an extended fork of [`frappe/ecommerce_integrations`](https://github.com/frappe/ecommerce_integrations) with a full-featured **Shopware 6** and **Medusa v2** integration, a shared delta-sync engine, catalogue categorisation, AI-powered product descriptions and vector search, plus German invoicing/order workflows. Targets **Frappe / ERPNext v16**.

## Supported Integrations

| Integration | Description | Docs |
|-------------|-------------|------|
| **Shopware 6** | Full bidirectional sync (products, orders, customers, status) | see below |
| **Medusa v2** | Product, order, customer and inventory sync with Medusa v2 (headless commerce) | see below |
| **Product Sync Engine** | Shared delta-sync engine for Shopware & Medusa | [`docs/product_sync.md`](docs/product_sync.md) |
| **Catalog Mirror** | 1:1 mirror of ERPNext Item Groups as storefront categories | [`docs/catalog_mirror.md`](docs/catalog_mirror.md) |
| **Smart Collections** | Rule-based product groupings (sale, bestsellers, themed listings …) | [`docs/smart_collections.md`](docs/smart_collections.md) |
| **RAG (vector search)** | Exports product data as embeddings to Pinecone for AI search assistants | see below |
| **AI Description** | AI-generated product descriptions, short copy and SEO text via Google Gemini | see below |
| Shopify | Shopify integration (unmodified upstream) | [User docs](https://docs.erpnext.com/docs/v13/user/manual/en/erpnext_integration/shopify_integration) |
| Unicommerce | Unicommerce integration (unmodified upstream) | [User docs](https://docs.erpnext.com/docs/v13/user/manual/en/erpnext_integration/unicommerce_integration) |
| Zenoti | Zenoti integration (unmodified upstream) | [User docs](https://docs.erpnext.com/docs/v13/user/manual/en/erpnext_integration/zenoti_integration) |
| Amazon | Amazon SP API integration (unmodified upstream) | [User docs](https://docs.erpnext.com/docs/v13/user/manual/en/erpnext_integration/amazon_integration) |

---

## Shopware 6 Integration

Full bidirectional sync between ERPNext and Shopware 6:

**Products (ERPNext → Shopware)**
- Simple products and variants (incl. configurator/options, attributes)
- Base price (net/gross, configurable per price list) plus MSRP/strike-through price
- Stock level per item (summed across warehouses)
- Image gallery incl. cover image, automatic media upload and reconciliation
- Filterable properties/property groups with configurable, sortable display order
- Freely configurable custom fields (Shopware `customFields`) — one ERPNext field maps centrally onto a Shopware field, no per-item maintenance
- Brand/manufacturer incl. logo and description
- Delivery time and restock time, with a global default as fallback
- SEO metadata (meta title, meta description)
- Categories via Catalog Mirror and/or Smart Collections
- Minimum order quantity / purchase steps, tax rates
- Per-item sales-channel visibility (with manual per-item exceptions)

**Products (Shopware → ERPNext, reverse import)**
- Products, variants, categories, images, prices, stock, properties, brand, delivery time, description, SEO and discount fields

**Orders & status**
- Order import from Shopware into ERPNext
- Order status is reported back to Shopware on delivery note, payment and invoice postings

**Customers**
- Customer matching, including the Leitweg-ID for German e-invoicing (XRechnung)

**Under the hood**
- Redis-backed queue for high-performance bulk operations
- Runs through the shared **Product Sync Engine** (see below) with hash-based delta detection — unchanged items never trigger an API call

**Setup:**
1. Open `Shopware Setting` in ERPNext
2. Enter your Shopware 6 Admin API credentials
3. Configure warehouse and tax mappings
4. Enable the sync options you need

---

## Medusa v2 Integration

Connects ERPNext to Medusa v2 (headless commerce):

- **Products**: export incl. price (net), metadata sourced from ecommerce properties, brand, image gallery
- **Orders**: import via webhook or scheduled sync
- **Customers**: bidirectional sync
- **Inventory**: stock pushed to Medusa locations
- **Status sync**: fulfillment and payment status updates
- **Sales channels**: per-item assignment
- Also runs through the shared **Product Sync Engine**

**Setup:**
1. Open `Medusa Setting` in ERPNext
2. Enter your Medusa v2 API key and base URL
3. Configure price list and warehouse mappings
4. Enable the sync options you need

---

## Product Sync Engine

The shared delta-sync engine for Shopware and Medusa (doctype `Ecommerce Product Sync`):

- **Scope** is configurable: the whole catalogue, one Item Group (with/without subtree), a Catalog Mirror, or a Smart Collection
- **Canonical hash** per item — only fields that actually changed are pushed; an unchanged catalogue produces zero backend writes on every run
- **Preview with field-level diff**: before a live push, a preview shows exactly what would change (incl. risk flags for large price/stock jumps)
- **Per-Sync schedule** (hourly, every 6 hours, daily, custom cron expression) or purely manual
- **Priority** for overlapping syncs, plus conflict resolution (manual review, last-wins, skip)
- **Per-item overrides**: pin, skip, or override name/price/description for individual items
- Batched bulk push against the respective backend API, with deterministic ids for repeatable, idempotent runs
- Detailed audit log per run (`Ecommerce Sync Run`, `Ecommerce Sync Error`)

---

## Catalog Mirror & Smart Collections

Backend category placement is split across two complementary modules.

**Catalog Mirror** keeps a 1:1 mirror of the ERPNext Item Group tree under one root in the backend (Shopware or Medusa). One mirror per (backend, root Item Group) pair; renames, moves and new Item Groups propagate on the next sync. Use this whenever the storefront's standard category structure should track ERPNext. See [`docs/catalog_mirror.md`](docs/catalog_mirror.md).

**Smart Collections** are rule-based ad-hoc groupings — `Sale`, `Bestseller`, themed listings — that do not follow the Item Group tree. A collection's rules resolve to a set of items, and each enabled target pushes those items onto a sales channel (Shopware) or product category (Medusa). See [`docs/smart_collections.md`](docs/smart_collections.md).

Per-item exceptions live on `Item.ecommerce_channel_overrides`: an `exclude` row wins over both modules, an `include` row injects a channel that neither would have produced. See [`docs/multi_shop_setup.md`](docs/multi_shop_setup.md) for the resolver precedence rules.

---

## RAG Integration (Vector Search)

Exports product data as vector embeddings to **Pinecone**, powering AI-driven storefront search assistants and chatbots:

- Automatic and manual bulk sync through a dedicated queue
- Filterable by Item Group and sellability
- Own audit log (`RAG Log`)

**Setup:**
1. Open `RAG Setting` in ERPNext
2. Enter your Pinecone API key
3. Configure sync filters and batch size

---

## AI Description (AI-Generated Product Copy)

Automatic generation of product descriptions, short copy, benefit lists and SEO text via **Google Gemini**:

- Long and short description, benefits, SEO meta description
- Scheduled batch processing (hourly, configurable interval) for items not yet processed
- Rate-limit handling for the Gemini API
- Own audit log (`AI Description Log`)
- Results automatically flow into the Shopware/Medusa push (description fields, custom fields)

**Setup:**
1. Open `AI Description Setting` in ERPNext
2. Enter your Google Gemini API key
3. Configure batch processing and interval

---

## Cross-Channel Features

- **Ecommerce Item**: canonical ID-mapping table between ERPNext items and backend ids (Shopware/Medusa), including variants
- **Ecommerce Channel Branding**: logo, sender address, IBAN/BIC, imprint and signature per sales channel — print formats and emails pull this data at render time
- **Ecommerce Channel Override**: manual per-item, per-channel visibility exceptions
- **Channel-aware notifications**: email dispatch (sender address, CC routing, e-invoicing) follows the originating channel of the document
- **German print formats**: Auftragsbestätigung (order confirmation), Bestellbestätigung, Rechnung (invoice), Versandbestätigung (shipping confirmation) — generic and customisable per operator, rendered via Frappe v16's built-in Chrome PDF generator
- **E-invoicing (XRechnung)**: the Leitweg-ID is automatically mirrored into the standard electronic-address field

---

## Installation

**For Shopware 6 / Medusa / RAG / AI Description (this fork):**

```bash
# Get the app from this fork
$ bench get-app ecommerce_integrations https://github.com/DrdotHouse2106/ecommerce_integrations.git --branch feat/multi-channel-integrations

# Install on a site
$ bench --site <your-site> install-app ecommerce_integrations
```

**For the plain upstream version (standard Frappe/ERPNext integrations):**

```bash
# Production
$ bench get-app ecommerce_integrations --branch main

# Development
$ bench get-app ecommerce_integrations --branch develop

# Install on a site
$ bench --site <your-site> install-app ecommerce_integrations
```

After installation, follow the documentation for each integration.

---

## Contributing

- For Shopware 6 / Medusa / RAG / AI Description: pull requests against the `feat/multi-channel-integrations` branch
- For upstream integrations: follow the [ERPNext contribution guidelines](https://github.com/frappe/erpnext/wiki/Contribution-Guidelines)

## Development Setup

- Enable developer mode
- For webhook testing: set `localtunnel_url` in `site_config.json` to an ngrok/localtunnel URL

## License

GNU GPL v3.0
