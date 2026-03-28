# Medusa v2 ERPNext Integration — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a bidirectional Medusa v2 integration into the existing `ecommerce_integrations` Frappe app, following the Shopware6 module as architectural reference.

**Architecture:** ERPNext connects to Medusa v2 via its Admin REST API using a Secret API Key (`x-medusa-access-token`). A lightweight Medusa plugin (subscriber) pushes events (order.placed, customer.created, etc.) to ERPNext webhook endpoints. ERPNext pulls product/inventory data on schedule and pushes status updates back to Medusa. The integration follows the same patterns as the existing Shopware6 module — class-based sync objects, decorator-based session management, bulk queue, and doc event hooks.

**Tech Stack:** Python 3.10+, Frappe Framework, ERPNext, Medusa v2 Admin API (REST/JSON), `requests` library

---

## Architecture Overview

```
ERPNext (Frappe)                          Medusa v2
┌──────────────────────┐                  ┌──────────────────────┐
│ medusa/ module        │                  │ Medusa Backend        │
│                      │  REST API         │                      │
│ connection.py ───────┼──────────────────►│ /admin/products      │
│ product_export.py    │  (API Key auth)   │ /admin/orders        │
│ inventory.py ────────┼──────────────────►│ /admin/customers     │
│ customer.py          │                   │ /admin/inventory-*   │
│                      │                   │                      │
│ webhook_handler.py ◄─┼──────────────────┤ subscriber plugin    │
│ order/order_sync.py  │  HTTP POST        │ (order.placed, etc.) │
│ status_sync.py ──────┼──────────────────►│ /admin/orders/{id}   │
└──────────────────────┘                  └──────────────────────┘
```

**Key Design Decisions:**
1. Medusa v2 has NO native webhooks — we build a small Medusa subscriber plugin that POSTs events to ERPNext
2. Auth uses `x-medusa-access-token` header (Secret API Key, created in Medusa Admin)
3. Follows Shopware6 module patterns: `temp_medusa_session` decorator, `MedusaProduct`/`MedusaCustomer`/`MedusaOrder` classes
4. Pricing: Medusa stores prices in **lowest currency unit** (cents). ERPNext stores in standard units. Conversion required.
5. Product sync is ERPNext → Medusa (ERPNext is master for products). Order sync is Medusa → ERPNext.

---

## File Structure

### ERPNext Side (`ecommerce_integrations/medusa/`)

```
ecommerce_integrations/medusa/
├── __init__.py
├── connection.py              # API client, auth, retry, temp_medusa_session decorator
├── constants.py               # Module name, field names, state maps, API paths
├── utils.py                   # Logging, data transformation helpers
│
├── doctype/
│   └── medusa_setting/
│       ├── medusa_setting.json     # DocType definition (Single)
│       ├── medusa_setting.py       # Controller with test_connection, refresh
│       └── __init__.py
│
├── customer.py                # MedusaCustomer class — sync Medusa↔ERPNext customers
├── product_export.py          # Product export ERPNext → Medusa
├── inventory.py               # Stock sync ERPNext → Medusa
│
├── order/
│   ├── __init__.py
│   ├── order_sync.py          # MedusaOrder class — Medusa → ERPNext Sales Order
│   ├── order_mapper.py        # Map Medusa order JSON → ERPNext SO fields
│   └── scheduled_sync.py      # Scheduled order polling (fallback if webhook fails)
│
├── status_sync.py             # ERPNext doc events → Medusa status updates
└── webhook_handler.py         # Receive events from Medusa subscriber plugin
```

### Medusa Side (`/srv/medusa/src/subscribers/`)

```
/srv/medusa/src/subscribers/
└── erpnext-webhook.ts         # Subscriber that POSTs events to ERPNext
```

### Hooks Integration (modify existing)

```
ecommerce_integrations/hooks.py   # Add medusa doc_events + scheduler_events
```

---

## Phase 1: Foundation (Tasks 1-5)

### Task 1: Create Medusa Setting DocType

**Files:**
- Create: `ecommerce_integrations/medusa/__init__.py`
- Create: `ecommerce_integrations/medusa/constants.py`
- Create: `ecommerce_integrations/medusa/doctype/__init__.py`
- Create: `ecommerce_integrations/medusa/doctype/medusa_setting/__init__.py`
- Create: `ecommerce_integrations/medusa/doctype/medusa_setting/medusa_setting.json`
- Create: `ecommerce_integrations/medusa/doctype/medusa_setting/medusa_setting.py`

- [ ] **Step 1: Create module init and constants**

```python
# ecommerce_integrations/medusa/__init__.py
# Medusa v2 Integration for ERPNext
```

```python
# ecommerce_integrations/medusa/constants.py
MODULE_NAME = "medusa"
SETTING_DOCTYPE = "Medusa Setting"
LOG_DOCTYPE = "Ecommerce Integration Log"

# Custom field names on ERPNext DocTypes
CUSTOMER_ID_FIELD = "medusa_customer_id"
ORDER_ID_FIELD = "medusa_order_id"
PRODUCT_ID_FIELD = "medusa_product_id"
VARIANT_ID_FIELD = "medusa_variant_id"
CATEGORY_ID_FIELD = "medusa_category_id"

# API paths
API_PRODUCTS = "/admin/products"
API_PRODUCT_VARIANTS = "/admin/products/{product_id}/variants"
API_ORDERS = "/admin/orders"
API_CUSTOMERS = "/admin/customers"
API_STOCK_LOCATIONS = "/admin/stock-locations"
API_INVENTORY_ITEMS = "/admin/inventory-items"
API_INVENTORY_LEVELS = "/admin/inventory-items/{id}/location-levels"
API_INVENTORY_LEVELS_BATCH = "/admin/inventory-items/location-levels/batch"
API_CATEGORIES = "/admin/product-categories"
API_COLLECTIONS = "/admin/collections"
API_FULFILLMENTS = "/admin/orders/{order_id}/fulfillments"
API_AUTH = "/auth/user/emailpass"

# Order status mapping (Medusa → ERPNext)
ORDER_STATUS_MAP = {
    "pending": "Draft",
    "completed": "Completed",
    "archived": "Closed",
    "canceled": "Cancelled",
    "requires_action": "On Hold",
}

# Payment status mapping
PAYMENT_STATUS_MAP = {
    "not_paid": "Unpaid",
    "awaiting": "Unpaid",
    "authorized": "Unpaid",
    "partially_authorized": "Partly Paid",
    "captured": "Paid",
    "partially_captured": "Partly Paid",
    "partially_refunded": "Partly Paid",
    "refunded": "Refunded",
    "canceled": "Cancelled",
    "requires_action": "Unpaid",
}

# Fulfillment status mapping
FULFILLMENT_STATUS_MAP = {
    "not_fulfilled": "Not Delivered",
    "partially_fulfilled": "Partly Delivered",
    "fulfilled": "Fully Delivered",
    "partially_shipped": "Partly Delivered",
    "shipped": "Shipped",
    "partially_delivered": "Partly Delivered",
    "delivered": "Delivered",
    "canceled": "Cancelled",
    "partially_returned": "Return Issued",
    "returned": "Return Issued",
}

# Events the Medusa subscriber should forward
WEBHOOK_EVENTS = [
    "order.placed",
    "order.updated",
    "order.canceled",
    "order.completed",
    "order.fulfillment_created",
    "order.fulfillment_canceled",
    "customer.created",
    "customer.updated",
    "product.created",
    "product.updated",
    "product.deleted",
]

# Price conversion: Medusa uses lowest currency unit (cents)
# e.g. $49.99 = 4999 in Medusa
MEDUSA_PRICE_FACTOR = 100
```

- [ ] **Step 2: Create Medusa Setting DocType JSON**

```json
// ecommerce_integrations/medusa/doctype/medusa_setting/medusa_setting.json
{
  "actions": [],
  "creation": "2026-03-28 00:00:00",
  "doctype": "DocType",
  "engine": "InnoDB",
  "field_order": [
    "enable_medusa",
    "column_break_enable",
    "section_break_connection",
    "medusa_url",
    "api_key",
    "column_break_connection",
    "test_connection_btn",
    "connection_status_html",
    "section_break_webhook",
    "webhook_secret",
    "webhook_url_info_html",
    "section_break_customer",
    "default_customer",
    "customer_group",
    "column_break_customer",
    "sync_customers",
    "section_break_company",
    "company",
    "cost_center",
    "column_break_company",
    "warehouse",
    "cash_bank_account",
    "section_break_price",
    "default_selling_price_list",
    "price_list_includes_tax",
    "column_break_price",
    "default_tax_rate",
    "import_prices_as_net",
    "section_break_order",
    "order_sync_frequency",
    "last_order_sync",
    "column_break_order",
    "sales_order_series",
    "section_break_tax",
    "default_sales_tax_template",
    "column_break_tax",
    "default_shipping_charges_account",
    "section_break_product",
    "upload_erpnext_items",
    "sync_item_on_update",
    "column_break_product",
    "category_sync_root",
    "section_break_inventory",
    "sync_inventory",
    "inventory_sync_frequency",
    "last_inventory_sync",
    "column_break_inventory",
    "medusa_stock_location_id",
    "section_break_shipping",
    "add_shipping_as_item",
    "shipping_item",
    "column_break_shipping",
    "discount_item"
  ],
  "fields": [
    {"fieldname": "enable_medusa", "label": "Enable Medusa v2", "fieldtype": "Check", "default": "0"},
    {"fieldname": "column_break_enable", "fieldtype": "Column Break"},
    {"fieldname": "section_break_connection", "label": "Connection Settings", "fieldtype": "Section Break"},
    {"fieldname": "medusa_url", "label": "Medusa Backend URL", "fieldtype": "Data", "reqd": 0, "description": "e.g. http://medusa-backend:9000"},
    {"fieldname": "api_key", "label": "Secret API Key", "fieldtype": "Password", "reqd": 0, "description": "Create in Medusa Admin: Settings > Secret API Keys"},
    {"fieldname": "column_break_connection", "fieldtype": "Column Break"},
    {"fieldname": "test_connection_btn", "label": "Test Connection", "fieldtype": "Button"},
    {"fieldname": "connection_status_html", "label": "Connection Status", "fieldtype": "HTML"},
    {"fieldname": "section_break_webhook", "label": "Webhook Settings", "fieldtype": "Section Break"},
    {"fieldname": "webhook_secret", "label": "Webhook Secret", "fieldtype": "Password", "description": "Shared secret for HMAC validation of incoming Medusa events"},
    {"fieldname": "webhook_url_info_html", "label": "Webhook Info", "fieldtype": "HTML", "options": "<div class='alert alert-info'><p>Configure the Medusa subscriber plugin to POST events to:</p><code>/api/method/ecommerce_integrations.medusa.webhook_handler.handle_medusa_event</code><p class='mt-2'>Set the same webhook secret in both Medusa (.env) and here.</p></div>"},
    {"fieldname": "section_break_customer", "label": "Customer Settings", "fieldtype": "Section Break"},
    {"fieldname": "default_customer", "label": "Default Customer", "fieldtype": "Link", "options": "Customer", "description": "Fallback for guest orders"},
    {"fieldname": "customer_group", "label": "Customer Group", "fieldtype": "Link", "options": "Customer Group"},
    {"fieldname": "column_break_customer", "fieldtype": "Column Break"},
    {"fieldname": "sync_customers", "label": "Sync Customers from Medusa", "fieldtype": "Check", "default": "1"},
    {"fieldname": "section_break_company", "label": "Company Settings", "fieldtype": "Section Break"},
    {"fieldname": "company", "label": "Company", "fieldtype": "Link", "options": "Company"},
    {"fieldname": "cost_center", "label": "Cost Center", "fieldtype": "Link", "options": "Cost Center"},
    {"fieldname": "column_break_company", "fieldtype": "Column Break"},
    {"fieldname": "warehouse", "label": "Default Warehouse", "fieldtype": "Link", "options": "Warehouse"},
    {"fieldname": "cash_bank_account", "label": "Cash/Bank Account", "fieldtype": "Link", "options": "Account"},
    {"fieldname": "section_break_price", "label": "Price Handling", "fieldtype": "Section Break"},
    {"fieldname": "default_selling_price_list", "label": "Default Selling Price List", "fieldtype": "Link", "options": "Price List"},
    {"fieldname": "price_list_includes_tax", "label": "Price List Includes Tax (Gross)", "fieldtype": "Check", "default": "0"},
    {"fieldname": "column_break_price", "fieldtype": "Column Break"},
    {"fieldname": "default_tax_rate", "label": "Default Tax Rate (%)", "fieldtype": "Float", "default": "19"},
    {"fieldname": "import_prices_as_net", "label": "Import Prices as Net (B2B)", "fieldtype": "Check", "default": "1"},
    {"fieldname": "section_break_order", "label": "Order Sync", "fieldtype": "Section Break"},
    {"fieldname": "order_sync_frequency", "label": "Order Sync Frequency (Minutes)", "fieldtype": "Select", "options": "5\n10\n15\n30\n60", "default": "60"},
    {"fieldname": "last_order_sync", "label": "Last Order Sync", "fieldtype": "Datetime", "read_only": 1, "hidden": 1},
    {"fieldname": "column_break_order", "fieldtype": "Column Break"},
    {"fieldname": "sales_order_series", "label": "Sales Order Series", "fieldtype": "Select"},
    {"fieldname": "section_break_tax", "label": "Tax Settings", "fieldtype": "Section Break"},
    {"fieldname": "default_sales_tax_template", "label": "Default Sales Tax Template", "fieldtype": "Link", "options": "Sales Taxes and Charges Template"},
    {"fieldname": "column_break_tax", "fieldtype": "Column Break"},
    {"fieldname": "default_shipping_charges_account", "label": "Shipping Charges Account", "fieldtype": "Link", "options": "Account"},
    {"fieldname": "section_break_product", "label": "Product Sync (ERPNext to Medusa)", "fieldtype": "Section Break"},
    {"fieldname": "upload_erpnext_items", "label": "Upload ERPNext Items to Medusa", "fieldtype": "Check", "default": "0"},
    {"fieldname": "sync_item_on_update", "label": "Sync Item on Update", "fieldtype": "Check", "default": "0"},
    {"fieldname": "column_break_product", "fieldtype": "Column Break"},
    {"fieldname": "category_sync_root", "label": "Category Sync Root", "fieldtype": "Link", "options": "Item Group", "default": "Products"},
    {"fieldname": "section_break_inventory", "label": "Inventory Sync", "fieldtype": "Section Break"},
    {"fieldname": "sync_inventory", "label": "Sync Inventory to Medusa", "fieldtype": "Check", "default": "0"},
    {"fieldname": "inventory_sync_frequency", "label": "Inventory Sync Frequency (Minutes)", "fieldtype": "Select", "options": "5\n10\n15\n30\n60", "default": "60"},
    {"fieldname": "last_inventory_sync", "label": "Last Inventory Sync", "fieldtype": "Datetime", "read_only": 1, "hidden": 1},
    {"fieldname": "column_break_inventory", "fieldtype": "Column Break"},
    {"fieldname": "medusa_stock_location_id", "label": "Medusa Stock Location ID", "fieldtype": "Data", "description": "The stock_location ID in Medusa to sync inventory to"},
    {"fieldname": "section_break_shipping", "label": "Shipping & Discounts", "fieldtype": "Section Break"},
    {"fieldname": "add_shipping_as_item", "label": "Add Shipping as Line Item", "fieldtype": "Check", "default": "1"},
    {"fieldname": "shipping_item", "label": "Shipping Item", "fieldtype": "Link", "options": "Item"},
    {"fieldname": "column_break_shipping", "fieldtype": "Column Break"},
    {"fieldname": "discount_item", "label": "Discount Item", "fieldtype": "Link", "options": "Item"}
  ],
  "index_web_pages_for_search": 0,
  "issingle": 1,
  "links": [],
  "modified": "2026-03-28 00:00:00",
  "modified_by": "Administrator",
  "module": "medusa",
  "name": "Medusa Setting",
  "owner": "Administrator",
  "permissions": [
    {"role": "System Manager", "read": 1, "write": 1, "create": 1, "delete": 1, "share": 1, "print": 1, "email": 1}
  ],
  "sort_field": "modified",
  "sort_order": "DESC",
  "track_changes": 1
}
```

- [ ] **Step 3: Create Medusa Setting controller**

```python
# ecommerce_integrations/medusa/doctype/medusa_setting/medusa_setting.py
import frappe
from frappe.model.document import Document


class MedusaSetting(Document):
    pass
```

- [ ] **Step 4: Commit**

```bash
git add ecommerce_integrations/medusa/
git commit -m "feat(medusa): add Medusa Setting DocType and constants"
```

---

### Task 2: Connection Module

**Files:**
- Create: `ecommerce_integrations/medusa/connection.py`
- Create: `ecommerce_integrations/medusa/utils.py`

- [ ] **Step 1: Create connection module**

```python
# ecommerce_integrations/medusa/connection.py
"""Medusa v2 API connection management.

Provides:
- get_medusa_session() for raw requests.Session with auth
- temp_medusa_session decorator that injects session + handles retries
- test_connection() for settings page
"""

import functools
import time

import frappe
import requests

from ecommerce_integrations.medusa.constants import SETTING_DOCTYPE


def get_medusa_session() -> tuple:
    """Return (requests.Session, base_url) configured with Medusa API Key auth.

    The session has the x-medusa-access-token header set.
    """
    setting = frappe.get_cached_doc(SETTING_DOCTYPE)
    if not setting.medusa_url or not setting.api_key:
        frappe.throw("Medusa URL and API Key must be configured in Medusa Setting")

    base_url = setting.medusa_url.rstrip("/")
    api_key = setting.get_password("api_key")

    session = requests.Session()
    session.headers.update({
        "Content-Type": "application/json",
        "Accept": "application/json",
        "x-medusa-access-token": api_key,
    })
    session.timeout = 60

    return session, base_url


def temp_medusa_session(func):
    """Decorator that injects (session, base_url) as first two args.

    Handles retry with exponential backoff on 502/503/504 gateway errors.
    """
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        session, base_url = get_medusa_session()
        max_retries = 3
        delay = 2.0

        for attempt in range(max_retries + 1):
            try:
                return func(session, base_url, *args, **kwargs)
            except requests.exceptions.HTTPError as e:
                if e.response is not None and e.response.status_code in (502, 503, 504) and attempt < max_retries:
                    time.sleep(delay)
                    delay *= 2.0
                    continue
                raise
            finally:
                session.close()
    return wrapper


def medusa_request(session, base_url, method, path, **kwargs):
    """Make an API request to Medusa and return parsed JSON.

    Raises requests.HTTPError on non-2xx responses.
    """
    url = f"{base_url}{path}"
    response = session.request(method, url, **kwargs)
    response.raise_for_status()
    if response.status_code == 204:
        return {}
    return response.json()


@frappe.whitelist()
def test_connection():
    """Test the Medusa API connection. Called from Medusa Setting form."""
    try:
        session, base_url = get_medusa_session()
        response = session.get(f"{base_url}/admin/products", params={"limit": 1})
        response.raise_for_status()
        session.close()
        return {"success": True, "message": "Connection successful"}
    except Exception as e:
        return {"success": False, "message": str(e)}
```

- [ ] **Step 2: Create utils module**

```python
# ecommerce_integrations/medusa/utils.py
"""Shared utilities for the Medusa integration."""

import frappe

from ecommerce_integrations.medusa.constants import (
    LOG_DOCTYPE,
    MEDUSA_PRICE_FACTOR,
    MODULE_NAME,
    SETTING_DOCTYPE,
)


def is_medusa_enabled() -> bool:
    """Check if Medusa integration is enabled."""
    try:
        return bool(frappe.db.get_single_value(SETTING_DOCTYPE, "enable_medusa"))
    except Exception:
        return False


def get_medusa_setting():
    """Get cached Medusa Setting document."""
    return frappe.get_cached_doc(SETTING_DOCTYPE)


def medusa_price_to_erpnext(amount_in_cents: int) -> float:
    """Convert Medusa price (cents/lowest unit) to ERPNext (standard unit).

    Medusa: 4999 (cents) → ERPNext: 49.99
    """
    if amount_in_cents is None:
        return 0.0
    return round(amount_in_cents / MEDUSA_PRICE_FACTOR, 2)


def erpnext_price_to_medusa(amount: float) -> int:
    """Convert ERPNext price (standard unit) to Medusa (cents/lowest unit).

    ERPNext: 49.99 → Medusa: 4999 (cents)
    """
    if amount is None:
        return 0
    return int(round(amount * MEDUSA_PRICE_FACTOR))


def create_medusa_log(
    request_type: str,
    status: str = "Queued",
    medusa_id: str = None,
    request_data: dict = None,
    response_data: dict = None,
    error: str = None,
) -> str:
    """Create an Ecommerce Integration Log entry for Medusa operations."""
    log = frappe.get_doc({
        "doctype": LOG_DOCTYPE,
        "integration": MODULE_NAME,
        "request_type": request_type,
        "status": status,
        "integration_item_code": medusa_id or "",
        "request_data": frappe.as_json(request_data) if request_data else "",
        "response_data": frappe.as_json(response_data) if response_data else "",
        "error": error or "",
    })
    log.insert(ignore_permissions=True)
    frappe.db.commit()
    return log.name


def update_medusa_log(log_name: str, **kwargs):
    """Update an existing Medusa log entry."""
    log = frappe.get_doc(LOG_DOCTYPE, log_name)
    for key, value in kwargs.items():
        if key == "response_data" and isinstance(value, dict):
            value = frappe.as_json(value)
        if key == "request_data" and isinstance(value, dict):
            value = frappe.as_json(value)
        log.set(key, value)
    log.save(ignore_permissions=True)
    frappe.db.commit()
```

- [ ] **Step 3: Commit**

```bash
git add ecommerce_integrations/medusa/connection.py ecommerce_integrations/medusa/utils.py
git commit -m "feat(medusa): add connection module and utils"
```

---

### Task 3: Webhook Handler (Medusa → ERPNext)

**Files:**
- Create: `ecommerce_integrations/medusa/webhook_handler.py`

- [ ] **Step 1: Create webhook handler**

```python
# ecommerce_integrations/medusa/webhook_handler.py
"""Handle incoming events from the Medusa subscriber plugin.

The Medusa subscriber POSTs JSON events to:
/api/method/ecommerce_integrations.medusa.webhook_handler.handle_medusa_event

Expected payload:
{
    "event": "order.placed",
    "data": {"id": "order_01ABC..."},
    "timestamp": "2026-03-28T12:00:00Z"
}
"""

import hashlib
import hmac
import json

import frappe

from ecommerce_integrations.medusa.constants import SETTING_DOCTYPE
from ecommerce_integrations.medusa.utils import create_medusa_log, is_medusa_enabled


@frappe.whitelist(allow_guest=True)
def handle_medusa_event():
    """Receive and process webhook events from Medusa subscriber plugin."""
    if not is_medusa_enabled():
        frappe.throw("Medusa integration is not enabled", frappe.AuthenticationError)

    payload = frappe.request.get_data(as_text=True)
    if not payload:
        frappe.throw("Empty payload", frappe.ValidationError)

    # Validate webhook signature
    setting = frappe.get_cached_doc(SETTING_DOCTYPE)
    webhook_secret = setting.get_password("webhook_secret") if setting.webhook_secret else None

    if webhook_secret:
        signature = frappe.request.headers.get("x-medusa-webhook-signature", "")
        expected = hmac.new(
            webhook_secret.encode("utf-8"),
            payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            frappe.throw("Invalid webhook signature", frappe.AuthenticationError)

    data = json.loads(payload)
    event_type = data.get("event", "")
    event_data = data.get("data", {})

    log_name = create_medusa_log(
        request_type=f"Webhook: {event_type}",
        status="Queued",
        medusa_id=event_data.get("id", ""),
        request_data=data,
    )

    try:
        _route_event(event_type, event_data)
        from ecommerce_integrations.medusa.utils import update_medusa_log
        update_medusa_log(log_name, status="Success")
    except Exception as e:
        from ecommerce_integrations.medusa.utils import update_medusa_log
        update_medusa_log(log_name, status="Error", error=str(e))
        frappe.log_error(f"Medusa webhook error: {event_type}", str(e))


def _route_event(event_type: str, data: dict):
    """Route event to the appropriate handler."""
    # Enqueue for background processing to avoid blocking the webhook response
    handler_map = {
        "order.placed": "ecommerce_integrations.medusa.order.order_sync.sync_order",
        "order.updated": "ecommerce_integrations.medusa.order.order_sync.sync_order",
        "order.canceled": "ecommerce_integrations.medusa.order.order_sync.sync_order",
        "order.completed": "ecommerce_integrations.medusa.order.order_sync.sync_order",
        "customer.created": "ecommerce_integrations.medusa.customer.sync_customer_by_id",
        "customer.updated": "ecommerce_integrations.medusa.customer.sync_customer_by_id",
    }

    handler = handler_map.get(event_type)
    if handler:
        frappe.enqueue(
            handler,
            queue="default",
            entity_id=data.get("id"),
            event_type=event_type,
            is_async=True,
        )
```

- [ ] **Step 2: Commit**

```bash
git add ecommerce_integrations/medusa/webhook_handler.py
git commit -m "feat(medusa): add webhook handler for Medusa events"
```

---

### Task 4: Medusa Subscriber Plugin (Medusa side)

**Files:**
- Create: `/srv/medusa/src/subscribers/erpnext-webhook.ts`

- [ ] **Step 1: Create the Medusa subscriber**

```typescript
// /srv/medusa/src/subscribers/erpnext-webhook.ts
import { SubscriberArgs, type SubscriberConfig } from "@medusajs/framework"
import { createHmac } from "crypto"

type EventPayload = { id: string }

const ERPNEXT_WEBHOOK_URL = process.env.ERPNEXT_WEBHOOK_URL || ""
const ERPNEXT_WEBHOOK_SECRET = process.env.ERPNEXT_WEBHOOK_SECRET || ""

async function sendToErpnext(eventName: string, data: EventPayload) {
  if (!ERPNEXT_WEBHOOK_URL) {
    return
  }

  const payload = JSON.stringify({
    event: eventName,
    data,
    timestamp: new Date().toISOString(),
  })

  const headers: Record<string, string> = {
    "Content-Type": "application/json",
  }

  if (ERPNEXT_WEBHOOK_SECRET) {
    const signature = createHmac("sha256", ERPNEXT_WEBHOOK_SECRET)
      .update(payload)
      .digest("hex")
    headers["x-medusa-webhook-signature"] = signature
  }

  try {
    const response = await fetch(ERPNEXT_WEBHOOK_URL, {
      method: "POST",
      headers,
      body: payload,
    })
    if (!response.ok) {
      console.error(`ERPNext webhook failed: ${response.status} ${await response.text()}`)
    }
  } catch (error) {
    console.error(`ERPNext webhook error for ${eventName}:`, error)
  }
}

export default async function erpnextWebhookHandler({
  event,
}: SubscriberArgs<EventPayload>) {
  await sendToErpnext(event.name, event.data)
}

export const config: SubscriberConfig = {
  event: [
    "order.placed",
    "order.updated",
    "order.canceled",
    "order.completed",
    "customer.created",
    "customer.updated",
  ],
}
```

- [ ] **Step 2: Add env vars to Medusa .env**

Add to `/srv/medusa/.env`:
```
ERPNEXT_WEBHOOK_URL=http://erpnext-frontend:8080/api/method/ecommerce_integrations.medusa.webhook_handler.handle_medusa_event
ERPNEXT_WEBHOOK_SECRET=your-shared-secret-here
```

- [ ] **Step 3: Commit**

```bash
cd /srv/medusa && git add src/subscribers/erpnext-webhook.ts .env
git commit -m "feat: add ERPNext webhook subscriber"
cd /srv/erpnextfrappe/ecommerce_integrations
```

---

### Task 5: Customer Sync

**Files:**
- Create: `ecommerce_integrations/medusa/customer.py`

- [ ] **Step 1: Create customer sync module**

```python
# ecommerce_integrations/medusa/customer.py
"""Bidirectional customer sync between Medusa v2 and ERPNext.

Medusa → ERPNext: Triggered by webhook (customer.created/updated)
ERPNext → Medusa: Optional, triggered on Customer save (if enabled)
"""

import frappe

from ecommerce_integrations.medusa.connection import medusa_request, temp_medusa_session
from ecommerce_integrations.medusa.constants import (
    API_CUSTOMERS,
    CUSTOMER_ID_FIELD,
    SETTING_DOCTYPE,
)
from ecommerce_integrations.medusa.utils import create_medusa_log, is_medusa_enabled, update_medusa_log


class MedusaCustomer:
    """Handles syncing a single Medusa customer to/from ERPNext."""

    def __init__(self, medusa_customer_id: str):
        self.medusa_id = medusa_customer_id
        self.setting = frappe.get_cached_doc(SETTING_DOCTYPE)

    def get_existing_customer(self):
        """Find existing ERPNext Customer linked to this Medusa ID."""
        name = frappe.db.get_value("Customer", {CUSTOMER_ID_FIELD: self.medusa_id})
        return name

    def is_synced(self) -> bool:
        return bool(self.get_existing_customer())

    @temp_medusa_session
    def fetch_from_medusa(self, session, base_url) -> dict:
        """Fetch full customer data from Medusa API."""
        return medusa_request(session, base_url, "GET", f"{API_CUSTOMERS}/{self.medusa_id}")

    def sync_to_erpnext(self, medusa_data: dict = None):
        """Create or update ERPNext Customer from Medusa data."""
        if medusa_data is None:
            result = self.fetch_from_medusa()
            medusa_data = result.get("customer", {})

        customer_name = self.get_existing_customer()

        if customer_name:
            self._update_customer(customer_name, medusa_data)
        else:
            customer_name = self._create_customer(medusa_data)

        return customer_name

    def _create_customer(self, data: dict) -> str:
        """Create a new ERPNext Customer from Medusa data."""
        company_name = data.get("company_name", "")
        first_name = data.get("first_name", "")
        last_name = data.get("last_name", "")
        email = data.get("email", "")

        # Determine customer name
        if company_name:
            cust_name = company_name
            customer_type = "Company"
        elif first_name or last_name:
            cust_name = f"{first_name} {last_name}".strip()
            customer_type = "Individual"
        else:
            cust_name = email
            customer_type = "Individual"

        customer = frappe.get_doc({
            "doctype": "Customer",
            "customer_name": cust_name,
            "customer_type": customer_type,
            "customer_group": self.setting.customer_group or "All Customer Groups",
            CUSTOMER_ID_FIELD: self.medusa_id,
        })
        customer.flags.ignore_mandatory = True
        customer.insert(ignore_permissions=True)

        # Create contact
        if email or data.get("phone"):
            self._create_contact(customer.name, data)

        # Create addresses
        for addr in data.get("addresses", []):
            self._create_address(customer.name, addr)

        frappe.db.commit()
        return customer.name

    def _update_customer(self, customer_name: str, data: dict):
        """Update existing ERPNext Customer with Medusa data."""
        customer = frappe.get_doc("Customer", customer_name)
        company_name = data.get("company_name", "")

        if company_name and company_name != customer.customer_name:
            customer.customer_name = company_name
        elif not company_name:
            name = f"{data.get('first_name', '')} {data.get('last_name', '')}".strip()
            if name and name != customer.customer_name:
                customer.customer_name = name

        customer.save(ignore_permissions=True)
        frappe.db.commit()

    def _create_contact(self, customer_name: str, data: dict):
        """Create a Contact linked to the customer."""
        contact = frappe.get_doc({
            "doctype": "Contact",
            "first_name": data.get("first_name", ""),
            "last_name": data.get("last_name", ""),
        })

        if data.get("email"):
            contact.append("email_ids", {"email_id": data["email"], "is_primary": 1})
        if data.get("phone"):
            contact.append("phone_nos", {"phone": data["phone"], "is_primary_phone": 1})

        contact.append("links", {
            "link_doctype": "Customer",
            "link_name": customer_name,
        })
        contact.flags.ignore_mandatory = True
        contact.insert(ignore_permissions=True)

    def _create_address(self, customer_name: str, addr: dict):
        """Create an Address linked to the customer."""
        address = frappe.get_doc({
            "doctype": "Address",
            "address_title": customer_name,
            "address_type": "Billing",
            "address_line1": addr.get("address_1", ""),
            "address_line2": addr.get("address_2", ""),
            "city": addr.get("city", ""),
            "state": addr.get("province", ""),
            "pincode": addr.get("postal_code", ""),
            "country": _get_country_name(addr.get("country_code", "DE")),
            "phone": addr.get("phone", ""),
        })
        address.append("links", {
            "link_doctype": "Customer",
            "link_name": customer_name,
        })
        address.flags.ignore_mandatory = True
        address.insert(ignore_permissions=True)


def _get_country_name(country_code: str) -> str:
    """Convert ISO country code to ERPNext country name."""
    if not country_code:
        return "Germany"
    name = frappe.db.get_value("Country", {"code": country_code.lower()})
    return name or "Germany"


def sync_customer_by_id(entity_id: str, event_type: str = ""):
    """Entry point for webhook-triggered customer sync."""
    if not is_medusa_enabled():
        return

    log_name = create_medusa_log(
        request_type=f"Customer Sync ({event_type})",
        medusa_id=entity_id,
        status="In Progress",
    )

    try:
        customer = MedusaCustomer(entity_id)
        customer_name = customer.sync_to_erpnext()
        update_medusa_log(log_name, status="Success", response_data={"customer": customer_name})
    except Exception as e:
        update_medusa_log(log_name, status="Error", error=str(e))
        frappe.log_error(f"Medusa customer sync failed: {entity_id}", str(e))
```

- [ ] **Step 2: Commit**

```bash
git add ecommerce_integrations/medusa/customer.py
git commit -m "feat(medusa): add customer sync (Medusa → ERPNext)"
```

---

## Phase 2: Order Sync (Tasks 6-7)

### Task 6: Order Sync (Medusa → ERPNext)

**Files:**
- Create: `ecommerce_integrations/medusa/order/__init__.py`
- Create: `ecommerce_integrations/medusa/order/order_sync.py`
- Create: `ecommerce_integrations/medusa/order/order_mapper.py`
- Create: `ecommerce_integrations/medusa/order/scheduled_sync.py`

- [ ] **Step 1: Create order sync module**

```python
# ecommerce_integrations/medusa/order/__init__.py
```

```python
# ecommerce_integrations/medusa/order/order_sync.py
"""Sync Medusa orders to ERPNext Sales Orders.

Triggered by:
1. Webhook: order.placed event from Medusa subscriber
2. Scheduled: Poll Medusa API for new orders (fallback)
"""

import frappe

from ecommerce_integrations.medusa.connection import medusa_request, temp_medusa_session
from ecommerce_integrations.medusa.constants import API_ORDERS, ORDER_ID_FIELD, SETTING_DOCTYPE
from ecommerce_integrations.medusa.customer import MedusaCustomer
from ecommerce_integrations.medusa.order.order_mapper import map_medusa_order_to_so
from ecommerce_integrations.medusa.utils import (
    create_medusa_log,
    is_medusa_enabled,
    update_medusa_log,
)


class MedusaOrder:
    """Encapsulates syncing a single Medusa order to ERPNext."""

    def __init__(self, order_id: str):
        self.order_id = order_id
        self.setting = frappe.get_cached_doc(SETTING_DOCTYPE)
        self.sales_order_name = self._get_existing_sales_order()

    def _get_existing_sales_order(self):
        return frappe.db.get_value("Sales Order", {ORDER_ID_FIELD: self.order_id})

    def is_synced(self) -> bool:
        return bool(self.sales_order_name)

    @temp_medusa_session
    def fetch_order(self, session, base_url) -> dict:
        """Fetch full order data from Medusa API."""
        result = medusa_request(
            session, base_url, "GET",
            f"{API_ORDERS}/{self.order_id}",
            params={"fields": "+items,+items.variant,+items.variant.product,+shipping_address,+billing_address,+customer,+shipping_methods,+transactions"},
        )
        return result.get("order", {})

    def sync(self) -> str:
        """Sync this order to ERPNext. Returns Sales Order name."""
        if self.is_synced():
            return self.sales_order_name

        order_data = self.fetch_order()
        if not order_data:
            frappe.throw(f"Could not fetch Medusa order {self.order_id}")

        # Ensure customer exists
        customer_id = order_data.get("customer_id")
        if customer_id and self.setting.sync_customers:
            mc = MedusaCustomer(customer_id)
            if not mc.is_synced():
                mc.sync_to_erpnext()

        # Map and create Sales Order
        so_data = map_medusa_order_to_so(order_data, self.setting)
        so = frappe.get_doc(so_data)
        so.flags.ignore_mandatory = True
        so.insert(ignore_permissions=True)
        so.submit()
        frappe.db.commit()

        self.sales_order_name = so.name
        return so.name


def sync_order(entity_id: str, event_type: str = ""):
    """Entry point for webhook/scheduled order sync."""
    if not is_medusa_enabled():
        return

    log_name = create_medusa_log(
        request_type=f"Order Sync ({event_type})",
        medusa_id=entity_id,
        status="In Progress",
    )

    try:
        order = MedusaOrder(entity_id)
        if order.is_synced():
            update_medusa_log(log_name, status="Skipped", response_data={"message": "Already synced"})
            return

        so_name = order.sync()
        update_medusa_log(log_name, status="Success", response_data={"sales_order": so_name})
    except Exception as e:
        update_medusa_log(log_name, status="Error", error=str(e))
        frappe.log_error(f"Medusa order sync failed: {entity_id}", str(e))
```

- [ ] **Step 2: Create order mapper**

```python
# ecommerce_integrations/medusa/order/order_mapper.py
"""Map Medusa order JSON to ERPNext Sales Order dict."""

import frappe

from ecommerce_integrations.medusa.constants import (
    CUSTOMER_ID_FIELD,
    ORDER_ID_FIELD,
    PRODUCT_ID_FIELD,
)
from ecommerce_integrations.medusa.utils import medusa_price_to_erpnext


def map_medusa_order_to_so(order: dict, setting) -> dict:
    """Transform a Medusa order dict into an ERPNext Sales Order dict."""
    customer_name = _resolve_customer(order, setting)
    items = _map_line_items(order.get("items", []), setting)
    shipping_items = _map_shipping(order, setting)

    so = {
        "doctype": "Sales Order",
        "naming_series": setting.sales_order_series or "SO-.#####",
        ORDER_ID_FIELD: order.get("id"),
        "customer": customer_name,
        "company": setting.company,
        "transaction_date": frappe.utils.today(),
        "delivery_date": frappe.utils.add_days(frappe.utils.today(), 7),
        "items": items + shipping_items,
        "cost_center": setting.cost_center,
        "set_warehouse": setting.warehouse,
    }

    # Tax template
    if setting.default_sales_tax_template:
        so["taxes_and_charges"] = setting.default_sales_tax_template

    # Shipping address
    shipping_addr = order.get("shipping_address", {})
    if shipping_addr:
        so["shipping_address_name"] = _get_or_create_address(shipping_addr, customer_name)

    return so


def _resolve_customer(order: dict, setting) -> str:
    """Find the ERPNext Customer for this order."""
    customer_id = order.get("customer_id")
    if customer_id:
        customer_name = frappe.db.get_value("Customer", {CUSTOMER_ID_FIELD: customer_id})
        if customer_name:
            return customer_name

    # Fallback to default customer
    if setting.default_customer:
        return setting.default_customer

    frappe.throw(f"No customer found for Medusa order {order.get('id')}")


def _map_line_items(items: list, setting) -> list:
    """Map Medusa order items to ERPNext Sales Order Items."""
    so_items = []

    for item in items:
        variant = item.get("variant", {}) or {}
        product = variant.get("product", {}) or {}
        product_id = product.get("id") or variant.get("product_id", "")

        # Find ERPNext item by medusa_product_id or SKU
        item_code = _resolve_item_code(product_id, variant.get("sku", ""), item)
        if not item_code:
            continue

        unit_price = medusa_price_to_erpnext(item.get("unit_price", 0))
        quantity = item.get("quantity", 1)

        so_item = {
            "item_code": item_code,
            "qty": quantity,
            "rate": unit_price,
            "warehouse": setting.warehouse,
        }

        # Apply discount if present
        discount = item.get("discount_total", 0)
        if discount:
            so_item["discount_amount"] = medusa_price_to_erpnext(discount)

        so_items.append(so_item)

    return so_items


def _map_shipping(order: dict, setting) -> list:
    """Map shipping costs as a line item if configured."""
    if not setting.add_shipping_as_item or not setting.shipping_item:
        return []

    shipping_total = 0
    for method in order.get("shipping_methods", []):
        shipping_total += method.get("amount", 0)

    if not shipping_total:
        return []

    return [{
        "item_code": setting.shipping_item,
        "qty": 1,
        "rate": medusa_price_to_erpnext(shipping_total),
        "warehouse": setting.warehouse,
    }]


def _resolve_item_code(product_id: str, sku: str, item: dict) -> str:
    """Find the ERPNext Item Code matching a Medusa product/variant."""
    # Try by Medusa product ID
    if product_id:
        item_code = frappe.db.get_value("Item", {PRODUCT_ID_FIELD: product_id})
        if item_code:
            return item_code

    # Try by SKU
    if sku:
        item_code = frappe.db.get_value("Item", {"item_code": sku})
        if item_code:
            return item_code

    # Try by item title
    title = item.get("title", "")
    if title:
        item_code = frappe.db.get_value("Item", {"item_name": title})
        if item_code:
            return item_code

    frappe.log_error(
        f"Could not resolve ERPNext item for Medusa product {product_id} / SKU {sku}",
        "Medusa Order Sync",
    )
    return None


def _get_or_create_address(addr: dict, customer_name: str) -> str:
    """Get or create an Address record from Medusa shipping address."""
    from ecommerce_integrations.medusa.customer import _get_country_name

    existing = frappe.db.get_value("Address", {
        "address_line1": addr.get("address_1", ""),
        "city": addr.get("city", ""),
        "pincode": addr.get("postal_code", ""),
    })
    if existing:
        return existing

    address = frappe.get_doc({
        "doctype": "Address",
        "address_title": f"{addr.get('first_name', '')} {addr.get('last_name', '')}".strip() or customer_name,
        "address_type": "Shipping",
        "address_line1": addr.get("address_1", ""),
        "address_line2": addr.get("address_2", ""),
        "city": addr.get("city", ""),
        "state": addr.get("province", ""),
        "pincode": addr.get("postal_code", ""),
        "country": _get_country_name(addr.get("country_code", "DE")),
        "phone": addr.get("phone", ""),
    })
    address.append("links", {"link_doctype": "Customer", "link_name": customer_name})
    address.flags.ignore_mandatory = True
    address.insert(ignore_permissions=True)
    return address.name
```

- [ ] **Step 3: Create scheduled order sync**

```python
# ecommerce_integrations/medusa/order/scheduled_sync.py
"""Scheduled order polling as fallback when webhooks fail."""

import frappe

from ecommerce_integrations.medusa.connection import medusa_request, temp_medusa_session
from ecommerce_integrations.medusa.constants import API_ORDERS, ORDER_ID_FIELD, SETTING_DOCTYPE
from ecommerce_integrations.medusa.order.order_sync import MedusaOrder
from ecommerce_integrations.medusa.utils import is_medusa_enabled


def sync_new_orders():
    """Scheduled job: fetch recent orders from Medusa and sync to ERPNext."""
    if not is_medusa_enabled():
        return

    setting = frappe.get_cached_doc(SETTING_DOCTYPE)
    since = setting.last_order_sync or frappe.utils.add_days(frappe.utils.now_datetime(), -1)

    orders = _fetch_orders_since(since)

    for order_data in orders:
        order_id = order_data.get("id")
        if not order_id:
            continue

        # Skip if already synced
        if frappe.db.exists("Sales Order", {ORDER_ID_FIELD: order_id}):
            continue

        try:
            order = MedusaOrder(order_id)
            order.sync()
        except Exception as e:
            frappe.log_error(f"Medusa scheduled order sync failed: {order_id}", str(e))

    # Update last sync timestamp
    frappe.db.set_single_value(SETTING_DOCTYPE, "last_order_sync", frappe.utils.now_datetime())
    frappe.db.commit()


@temp_medusa_session
def _fetch_orders_since(session, base_url, since) -> list:
    """Fetch orders from Medusa created after `since` datetime."""
    result = medusa_request(
        session, base_url, "GET", API_ORDERS,
        params={
            "created_at[$gte]": str(since),
            "limit": 100,
            "order": "-created_at",
        },
    )
    return result.get("orders", [])
```

- [ ] **Step 4: Commit**

```bash
git add ecommerce_integrations/medusa/order/
git commit -m "feat(medusa): add order sync (Medusa → ERPNext Sales Order)"
```

---

### Task 7: Product Export (ERPNext → Medusa)

**Files:**
- Create: `ecommerce_integrations/medusa/product_export.py`

- [ ] **Step 1: Create product export module**

```python
# ecommerce_integrations/medusa/product_export.py
"""Export ERPNext Items to Medusa v2 as Products.

Triggered by:
1. Item after_insert / on_update hooks (if enabled)
2. Manual sync from Medusa Setting
"""

import frappe

from ecommerce_integrations.medusa.connection import medusa_request, temp_medusa_session
from ecommerce_integrations.medusa.constants import (
    API_PRODUCTS,
    API_PRODUCT_VARIANTS,
    PRODUCT_ID_FIELD,
    SETTING_DOCTYPE,
    VARIANT_ID_FIELD,
)
from ecommerce_integrations.medusa.utils import (
    create_medusa_log,
    erpnext_price_to_medusa,
    is_medusa_enabled,
    update_medusa_log,
)


class MedusaProductExporter:
    """Export a single ERPNext Item to Medusa."""

    def __init__(self, item_code: str):
        self.item_code = item_code
        self.item = frappe.get_doc("Item", item_code)
        self.setting = frappe.get_cached_doc(SETTING_DOCTYPE)

    def get_medusa_product_id(self):
        return self.item.get(PRODUCT_ID_FIELD)

    def is_synced(self) -> bool:
        return bool(self.get_medusa_product_id())

    def export(self):
        """Create or update product in Medusa."""
        if self.is_synced():
            self._update_product()
        else:
            self._create_product()

    @temp_medusa_session
    def _create_product(self, session, base_url):
        """Create a new product in Medusa."""
        payload = self._build_product_payload()

        result = medusa_request(session, base_url, "POST", API_PRODUCTS, json=payload)
        product = result.get("product", {})
        medusa_id = product.get("id")

        if medusa_id:
            frappe.db.set_value("Item", self.item_code, PRODUCT_ID_FIELD, medusa_id)

            # Store variant ID if created
            variants = product.get("variants", [])
            if variants:
                frappe.db.set_value("Item", self.item_code, VARIANT_ID_FIELD, variants[0].get("id"))

            frappe.db.commit()

    @temp_medusa_session
    def _update_product(self, session, base_url):
        """Update existing product in Medusa."""
        medusa_id = self.get_medusa_product_id()
        payload = self._build_product_payload(is_update=True)

        medusa_request(session, base_url, "PUT", f"{API_PRODUCTS}/{medusa_id}", json=payload)

    def _build_product_payload(self, is_update=False) -> dict:
        """Build the Medusa product JSON from ERPNext Item."""
        price = self._get_selling_price()

        payload = {
            "title": self.item.item_name,
            "description": self.item.description or self.item.item_name,
            "status": "published" if not self.item.disabled else "draft",
            "is_giftcard": False,
            "discountable": True,
        }

        if not is_update:
            # Variants and options only on create
            payload["options"] = [{"title": "Default", "values": ["Default"]}]
            payload["variants"] = [{
                "title": self.item.item_name,
                "sku": self.item.item_code,
                "manage_inventory": True,
                "allow_backorder": False,
                "prices": [{
                    "currency_code": frappe.db.get_default("currency").lower() or "eur",
                    "amount": erpnext_price_to_medusa(price),
                }],
            }]

        # Weight
        if self.item.weight_per_unit:
            payload["weight"] = int(self.item.weight_per_unit * 1000)  # kg to grams

        return payload

    def _get_selling_price(self) -> float:
        """Get selling price from configured price list."""
        price_list = self.setting.default_selling_price_list
        if not price_list:
            return 0.0

        price = frappe.db.get_value(
            "Item Price",
            {"item_code": self.item_code, "price_list": price_list, "selling": 1},
            "price_list_rate",
        )
        return price or 0.0


def upload_item_to_medusa(doc, method=None):
    """Hook: Item after_insert / on_update. Exports item to Medusa if enabled."""
    if not is_medusa_enabled():
        return

    setting = frappe.get_cached_doc(SETTING_DOCTYPE)
    if method == "after_insert" and not setting.upload_erpnext_items:
        return
    if method == "on_update" and not setting.sync_item_on_update:
        return

    try:
        exporter = MedusaProductExporter(doc.name)
        exporter.export()
    except Exception as e:
        frappe.log_error(f"Medusa product export failed: {doc.name}", str(e))
```

- [ ] **Step 2: Commit**

```bash
git add ecommerce_integrations/medusa/product_export.py
git commit -m "feat(medusa): add product export (ERPNext → Medusa)"
```

---

## Phase 3: Inventory & Status Sync (Tasks 8-9)

### Task 8: Inventory Sync (ERPNext → Medusa)

**Files:**
- Create: `ecommerce_integrations/medusa/inventory.py`

- [ ] **Step 1: Create inventory sync module**

```python
# ecommerce_integrations/medusa/inventory.py
"""Sync ERPNext stock levels to Medusa v2 inventory.

Triggered by:
1. Scheduled job (configurable frequency)
2. Stock Entry / Stock Reconciliation submit hooks
"""

import frappe

from ecommerce_integrations.medusa.connection import medusa_request, temp_medusa_session
from ecommerce_integrations.medusa.constants import (
    API_INVENTORY_LEVELS_BATCH,
    PRODUCT_ID_FIELD,
    SETTING_DOCTYPE,
    VARIANT_ID_FIELD,
)
from ecommerce_integrations.medusa.utils import is_medusa_enabled


def sync_inventory_to_medusa():
    """Scheduled job: sync all stock levels from ERPNext to Medusa."""
    if not is_medusa_enabled():
        return

    setting = frappe.get_cached_doc(SETTING_DOCTYPE)
    if not setting.sync_inventory:
        return

    if not setting.medusa_stock_location_id:
        frappe.log_error("Medusa stock location ID not configured", "Medusa Inventory Sync")
        return

    # Get all items with a medusa_variant_id
    items = frappe.db.get_all(
        "Item",
        filters={VARIANT_ID_FIELD: ["is", "set"]},
        fields=["item_code", VARIANT_ID_FIELD],
    )

    if not items:
        return

    updates = []
    for item in items:
        stock_qty = _get_stock_qty(item["item_code"], setting.warehouse)
        updates.append({
            "inventory_item_id": item[VARIANT_ID_FIELD],
            "location_id": setting.medusa_stock_location_id,
            "stocked_quantity": max(0, int(stock_qty)),
        })

    if updates:
        _batch_update_inventory(updates)

    frappe.db.set_single_value(SETTING_DOCTYPE, "last_inventory_sync", frappe.utils.now_datetime())
    frappe.db.commit()


@temp_medusa_session
def _batch_update_inventory(session, base_url, updates: list):
    """Send batch inventory update to Medusa."""
    # Medusa batch endpoint accepts create and update in one call
    medusa_request(
        session, base_url, "POST",
        API_INVENTORY_LEVELS_BATCH,
        json={"update": updates},
    )


def _get_stock_qty(item_code: str, warehouse: str) -> float:
    """Get actual stock quantity from ERPNext."""
    if not warehouse:
        return 0.0
    return frappe.db.get_value(
        "Bin",
        {"item_code": item_code, "warehouse": warehouse},
        "actual_qty",
    ) or 0.0


def update_stock_on_stock_entry(doc, method=None):
    """Hook: Stock Entry on_submit. Trigger inventory sync for affected items."""
    if not is_medusa_enabled():
        return

    setting = frappe.get_cached_doc(SETTING_DOCTYPE)
    if not setting.sync_inventory:
        return

    for item in doc.items:
        if frappe.db.get_value("Item", item.item_code, VARIANT_ID_FIELD):
            frappe.enqueue(
                "ecommerce_integrations.medusa.inventory.sync_inventory_to_medusa",
                queue="default",
                is_async=True,
            )
            break  # One sync covers all items


def update_stock_on_stock_reconciliation(doc, method=None):
    """Hook: Stock Reconciliation on_submit."""
    update_stock_on_stock_entry(doc, method)
```

- [ ] **Step 2: Commit**

```bash
git add ecommerce_integrations/medusa/inventory.py
git commit -m "feat(medusa): add inventory sync (ERPNext → Medusa)"
```

---

### Task 9: Status Sync (ERPNext → Medusa)

**Files:**
- Create: `ecommerce_integrations/medusa/status_sync.py`

- [ ] **Step 1: Create status sync module**

```python
# ecommerce_integrations/medusa/status_sync.py
"""Push ERPNext document status changes to Medusa v2.

Triggered by doc_events on Sales Order, Delivery Note, Sales Invoice.
"""

import frappe

from ecommerce_integrations.medusa.connection import medusa_request, temp_medusa_session
from ecommerce_integrations.medusa.constants import API_FULFILLMENTS, API_ORDERS, ORDER_ID_FIELD, SETTING_DOCTYPE
from ecommerce_integrations.medusa.utils import is_medusa_enabled


def on_sales_order_cancel(doc, method=None):
    """Hook: Sales Order on_cancel. Cancel order in Medusa."""
    if not is_medusa_enabled():
        return

    medusa_order_id = doc.get(ORDER_ID_FIELD)
    if not medusa_order_id:
        return

    try:
        _cancel_medusa_order(medusa_order_id)
    except Exception as e:
        frappe.log_error(f"Failed to cancel Medusa order {medusa_order_id}", str(e))


def on_delivery_note_submit(doc, method=None):
    """Hook: Delivery Note on_submit. Create fulfillment in Medusa."""
    if not is_medusa_enabled():
        return

    # Find linked Sales Order with medusa_order_id
    for item in doc.items:
        if item.against_sales_order:
            medusa_order_id = frappe.db.get_value(
                "Sales Order", item.against_sales_order, ORDER_ID_FIELD
            )
            if medusa_order_id:
                try:
                    _create_medusa_fulfillment(medusa_order_id, doc)
                except Exception as e:
                    frappe.log_error(f"Failed to create Medusa fulfillment for {medusa_order_id}", str(e))
                break


@temp_medusa_session
def _cancel_medusa_order(session, base_url, medusa_order_id: str):
    """Cancel an order in Medusa."""
    medusa_request(
        session, base_url, "POST",
        f"{API_ORDERS}/{medusa_order_id}/cancel",
    )


@temp_medusa_session
def _create_medusa_fulfillment(session, base_url, medusa_order_id: str, delivery_note):
    """Create a fulfillment in Medusa for a delivered order."""
    # Fetch order to get item IDs
    result = medusa_request(
        session, base_url, "GET",
        f"{API_ORDERS}/{medusa_order_id}",
        params={"fields": "+items"},
    )
    order = result.get("order", {})
    order_items = order.get("items", [])

    if not order_items:
        return

    items = [{"id": item["id"], "quantity": item["quantity"]} for item in order_items]

    medusa_request(
        session, base_url, "POST",
        API_FULFILLMENTS.format(order_id=medusa_order_id),
        json={"items": items},
    )
```

- [ ] **Step 2: Commit**

```bash
git add ecommerce_integrations/medusa/status_sync.py
git commit -m "feat(medusa): add status sync (ERPNext → Medusa)"
```

---

## Phase 4: Integration (Task 10)

### Task 10: Register Hooks and Custom Fields

**Files:**
- Modify: `ecommerce_integrations/hooks.py`
- Create: `ecommerce_integrations/medusa/custom_fields.py`

- [ ] **Step 1: Add Medusa custom fields to ERPNext DocTypes**

```python
# ecommerce_integrations/medusa/custom_fields.py
"""Install custom fields for Medusa integration on ERPNext DocTypes."""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

MEDUSA_CUSTOM_FIELDS = {
    "Customer": [
        {
            "fieldname": "medusa_customer_id",
            "label": "Medusa Customer ID",
            "fieldtype": "Data",
            "read_only": 1,
            "insert_after": "customer_name",
            "no_copy": 1,
            "print_hide": 1,
        },
    ],
    "Sales Order": [
        {
            "fieldname": "medusa_order_id",
            "label": "Medusa Order ID",
            "fieldtype": "Data",
            "read_only": 1,
            "insert_after": "title",
            "no_copy": 1,
            "print_hide": 1,
        },
    ],
    "Item": [
        {
            "fieldname": "medusa_product_id",
            "label": "Medusa Product ID",
            "fieldtype": "Data",
            "read_only": 1,
            "insert_after": "item_name",
            "no_copy": 1,
            "print_hide": 1,
        },
        {
            "fieldname": "medusa_variant_id",
            "label": "Medusa Variant ID",
            "fieldtype": "Data",
            "read_only": 1,
            "insert_after": "medusa_product_id",
            "no_copy": 1,
            "print_hide": 1,
        },
    ],
}


def setup_custom_fields():
    """Create all custom fields for the Medusa integration."""
    create_custom_fields(MEDUSA_CUSTOM_FIELDS, update=True)
```

- [ ] **Step 2: Add Medusa hooks to main hooks.py**

Add the following entries to `ecommerce_integrations/hooks.py`:

In `doc_events["Item"]["after_insert"]` list, add:
```python
"ecommerce_integrations.medusa.product_export.upload_item_to_medusa",
```

In `doc_events["Item"]["on_update"]` list, add:
```python
"ecommerce_integrations.medusa.product_export.upload_item_to_medusa",
```

In `doc_events["Sales Order"]`, add:
```python
"on_cancel": [
    # ... existing entries ...
    "ecommerce_integrations.medusa.status_sync.on_sales_order_cancel",
],
```

In `doc_events["Delivery Note"]`, add:
```python
"on_submit": [
    # ... existing entries ...
    "ecommerce_integrations.medusa.status_sync.on_delivery_note_submit",
],
```

In `doc_events["Stock Entry"]["on_submit"]` list, add:
```python
"ecommerce_integrations.medusa.inventory.update_stock_on_stock_entry",
```

In `doc_events["Stock Reconciliation"]`, add:
```python
"on_submit": [
    # ... existing entries ...
    "ecommerce_integrations.medusa.inventory.update_stock_on_stock_reconciliation",
],
```

In `scheduler_events["all"]` list, add:
```python
"ecommerce_integrations.medusa.inventory.sync_inventory_to_medusa",
"ecommerce_integrations.medusa.order.scheduled_sync.sync_new_orders",
```

- [ ] **Step 3: Add fixtures hook for custom fields installation**

Add to hooks.py:
```python
after_install = "ecommerce_integrations.medusa.custom_fields.setup_custom_fields"
```

Or add a patch file to run on next migrate.

- [ ] **Step 4: Commit**

```bash
git add ecommerce_integrations/medusa/custom_fields.py ecommerce_integrations/hooks.py
git commit -m "feat(medusa): register hooks, custom fields, and scheduler events"
```

---

## Summary

| Phase | Tasks | What it delivers |
|-------|-------|-----------------|
| **1: Foundation** | 1-5 | Settings DocType, API connection, webhook handler, Medusa subscriber, customer sync |
| **2: Orders** | 6-7 | Order sync (Medusa→ERPNext), product export (ERPNext→Medusa) |
| **3: Inventory & Status** | 8-9 | Stock sync, fulfillment/cancel status sync |
| **4: Integration** | 10 | Hooks registration, custom fields on standard DocTypes |

### Data Flow Summary

```
ERPNext Item (save) ──hook──► product_export.py ──POST──► Medusa /admin/products
ERPNext Stock (change) ──hook──► inventory.py ──POST──► Medusa /admin/inventory-items/batch
ERPNext DN (submit) ──hook──► status_sync.py ──POST──► Medusa /admin/orders/{id}/fulfillments
ERPNext SO (cancel) ──hook──► status_sync.py ──POST──► Medusa /admin/orders/{id}/cancel

Medusa order.placed ──subscriber──► webhook_handler.py ──enqueue──► order_sync.py → Sales Order
Medusa customer.created ──subscriber──► webhook_handler.py ──enqueue──► customer.py → Customer
Medusa (scheduled) ──cron──► scheduled_sync.py ──GET──► Medusa /admin/orders → Sales Orders
```
