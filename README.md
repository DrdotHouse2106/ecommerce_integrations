<div align="center">
    <img src="https://frappecloud.com/files/ERPNext%20-%20Ecommerce%20Integrations.png" height="128">
    <h2>Ecommerce Integrations for ERPNext</h2>

[![CI](https://github.com/frappe/ecommerce_integrations/actions/workflows/ci.yml/badge.svg)](https://github.com/frappe/ecommerce_integrations/actions/workflows/ci.yml)

</div>

> **Note:** This is an extended fork with Shopware 6, Medusa v2, RAG (AI-powered product search) and German E-Invoice support.

### Supported Integrations

| Integration | Description | Documentation |
|-------------|-------------|---------------|
| **Shopware 6** | Full bidirectional sync with Shopware 6 | See below |
| **Medusa v2** | Product, order, customer and inventory sync with Medusa v2 | See below |
| **RAG** | AI-powered product search (FAQ, assistant integration) | See below |
| Shopify | Shopify integration | [User docs](https://docs.erpnext.com/docs/v13/user/manual/en/erpnext_integration/shopify_integration) |
| Unicommerce | Unicommerce integration | [User docs](https://docs.erpnext.com/docs/v13/user/manual/en/erpnext_integration/unicommerce_integration) |
| Zenoti | Zenoti integration | [User docs](https://docs.erpnext.com/docs/v13/user/manual/en/erpnext_integration/zenoti_integration) |
| Amazon | Amazon SP API integration | [User docs](https://docs.erpnext.com/docs/v13/user/manual/en/erpnext_integration/amazon_integration) |


### Shopware 6 Integration

Full bidirectional sync between ERPNext and Shopware 6:

- **Products**: Items, prices, stock, images, properties, custom fields
- **Orders**: Import orders from Shopware to ERPNext
- **Customers**: Sync customer data
- **Status Sync**: Delivery notes, payments, invoices update Shopware order status
- **Bulk Sync**: Redis queue for high-performance bulk operations
- **Flexible Properties**: Key-value table for unlimited product attributes

**Setup:**
1. Go to `Shopware Setting` in ERPNext
2. Enter your Shopware 6 API credentials (Admin API)
3. Configure warehouse and tax mappings
4. Enable sync options as needed


### Medusa v2 Integration

Sync ERPNext with Medusa v2 headless commerce:

- **Products**: Export items with prices, metadata from ecommerce_properties
- **Orders**: Import orders from Medusa via webhooks or scheduled sync
- **Customers**: Bidirectional customer sync
- **Inventory**: Stock level sync to Medusa locations
- **Status Sync**: Order fulfillment and payment status updates

**Setup:**
1. Go to `Medusa Setting` in ERPNext
2. Enter your Medusa v2 API key and base URL
3. Configure price list and warehouse mappings
4. Enable sync options as needed


### RAG Integration (AI Product Search)

AI-powered product search and FAQ generation for shop assistants:

- FAQ field sync to Shopware custom fields
- AI-generated product descriptions
- Integration with AI assistants and chatbots

**Setup:**
1. Go to `Shopware Setting` > AI/RAG section in ERPNext
2. Configure your AI service endpoint
3. Enable FAQ and description sync


### Installation

**For Shopware6/RAG users (this fork):**

```bash
# Install from this fork
$ bench get-app ecommerce_integrations https://github.com/TubaApollo/ecommerce_integrations --branch shopware6-clean

# Install on site
$ bench --site sitename install-app ecommerce_integrations
```

**For upstream (Frappe Cloud/standard integrations):**

```bash
# Production
$ bench get-app ecommerce_integrations --branch main

# Development
$ bench get-app ecommerce_integrations --branch develop

# Install on site
$ bench --site sitename install-app ecommerce_integrations
```

After installation, follow the documentation for each integration.


### Contributing

- For Shopware6/Medusa/RAG: PRs to `shopware6-clean` branch
- For upstream integrations: Follow [ERPNext contribution guidelines](https://github.com/frappe/erpnext/wiki/Contribution-Guidelines)


### Development Setup

- Enable developer mode
- For webhook testing: Set `localtunnel_url` in site_config with ngrok/localtunnel URL


#### License

GNU GPL v3.0
