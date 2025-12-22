# Shopware Custom Fields Setup Guide

This document describes the custom fields that need to be added to your Shopware 6 installation and frontend theme for the ERPNext integration to work properly.

## Overview

The integration supports the following checkout custom fields:

| Feature | Shopware Field | ERPNext Field | Description |
|---------|---------------|---------------|-------------|
| Customer PO Number | `custom_po_number` | `customer_po_no` | Customer's internal order reference |
| Tel. Avis | `custom_tel_avis` | `tel_avis_requested` | Telephone notification before delivery |
| Forklift/Hebebühne | `custom_forklift_required` | `forklift_requested` | Lifting platform required for delivery |
| Invoice Email | `invoice_email` | `invoice_email` (Customer) | Alternative email for invoices |

---

## 1. Setting Up Custom Fields in Shopware Admin

### Step 1: Create a Custom Field Set

1. Go to **Settings > System > Custom Fields**
2. Click **Add Custom Field Set**
3. Configure:
   - **Technical Name**: `checkout_fields` or `erpnext_checkout`
   - **Position**: Order
   - **Active**: Yes

### Step 2: Add Custom Fields

Add the following custom fields to the set:

#### a) Customer PO Number (Bestellnummer)

```
Technical Name: custom_po_number
Type: Text field
Label (DE): Ihre Bestellnummer / Kommission
Label (EN): Your Order Reference / Commission
Position: 1
Required: No
Placeholder: z.B. Auftrag-2025-001
Help Text: Optional: Geben Sie Ihre interne Bestellnummer an
```

#### b) Telephone Avis (Telefonisches Avis)

```
Technical Name: custom_tel_avis
Type: Switch (Checkbox)
Label (DE): Tel. Avis gewünscht (+7,50 €)
Label (EN): Telephone notification required (+7.50 €)
Position: 2
Required: No
Help Text: Wir rufen Sie vor der Zustellung an
```

#### c) Forklift Required (Hebebühne)

```
Technical Name: custom_forklift_required
Type: Switch (Checkbox)
Label (DE): Hebebühne erforderlich
Label (EN): Forklift required
Position: 3
Required: No
Help Text: Bei schweren/sperrigen Artikeln
```

#### d) Invoice Email (Rechnungs-E-Mail)

```
Technical Name: invoice_email
Type: Text field (Email)
Label (DE): Abweichende Rechnungs-E-Mail
Label (EN): Alternative Invoice Email
Position: 4
Required: No
Placeholder: buchhaltung@firma.de
Help Text: Falls Rechnungen an eine andere Adresse gehen sollen
```

---

## 2. Adding Fields to Checkout (Theme Integration)

### Option A: Using Shopware's Custom Field Display

Custom fields can be automatically displayed in checkout if you enable them in the Custom Field Set configuration.

### Option B: Manual Theme Integration

If you need more control, add the fields manually to your theme's checkout template.

#### Twig Template Example

Create or modify `storefront/page/checkout/confirm/confirm-order.html.twig`:

```twig
{% sw_extends '@Storefront/storefront/page/checkout/confirm/confirm-order.html.twig' %}

{% block page_checkout_confirm_form_submit %}
    <div class="checkout-custom-fields card mb-3">
        <div class="card-body">
            <h5 class="card-title">{{ "checkout.additionalOptions"|trans }}</h5>
            
            {# Customer PO Number #}
            <div class="form-group mb-3">
                <label for="customPoNumber">
                    {{ "checkout.customerReference"|trans|default("Ihre Bestellnummer / Kommission") }}
                </label>
                <input type="text" 
                       class="form-control" 
                       id="customPoNumber" 
                       name="customFields[custom_po_number]"
                       placeholder="z.B. Auftrag-2025-001">
                <small class="form-text text-muted">
                    Optional: Geben Sie Ihre interne Bestellnummer an
                </small>
            </div>
            
            {# Tel. Avis Checkbox #}
            <div class="form-check mb-3">
                <input type="checkbox" 
                       class="form-check-input" 
                       id="telAvis" 
                       name="customFields[custom_tel_avis]"
                       value="1">
                <label class="form-check-label" for="telAvis">
                    {{ "checkout.telAvis"|trans|default("Tel. Avis gewünscht") }}
                    <span class="text-primary">(+7,50 €)</span>
                </label>
                <small class="form-text text-muted d-block">
                    Wir rufen Sie vor der Zustellung an
                </small>
            </div>
            
            {# Forklift Checkbox #}
            <div class="form-check mb-3">
                <input type="checkbox" 
                       class="form-check-input" 
                       id="forkliftRequired" 
                       name="customFields[custom_forklift_required]"
                       value="1">
                <label class="form-check-label" for="forkliftRequired">
                    {{ "checkout.forkliftRequired"|trans|default("Hebebühne erforderlich") }}
                </label>
                <small class="form-text text-muted d-block">
                    Bei schweren/sperrigen Artikeln
                </small>
            </div>
            
            {# Invoice Email #}
            <div class="form-group mb-3">
                <label for="invoiceEmail">
                    {{ "checkout.invoiceEmail"|trans|default("Abweichende Rechnungs-E-Mail") }}
                </label>
                <input type="email" 
                       class="form-control" 
                       id="invoiceEmail" 
                       name="customFields[invoice_email]"
                       placeholder="buchhaltung@firma.de">
                <small class="form-text text-muted">
                    Falls Rechnungen an eine andere Adresse gehen sollen
                </small>
            </div>
        </div>
    </div>
    
    {{ parent() }}
{% endblock %}
```

---

## 3. Service Products Setup

### In Shopware (Optional)

If you want Tel. Avis and Forklift to be actual products in Shopware:

#### Tel. Avis Product

```
Product Number: SERVICE-TEL-AVIS
Name: Service: Telefonisches Avis
Price: 7,50 € (brutto)
Tax Rate: 19%
Active: Yes
Stock: 999999 (or disable stock management)
Categories: Services
Custom Fields:
  - is_service_product: true
```

#### Forklift Product

```
Product Number: SERVICE-FORKLIFT
Name: Service: Hebebühne / Forklift
Price: 0,00 € (or your desired rate)
Tax Rate: 19%
Active: Yes
Stock: 999999 (or disable stock management)
Categories: Services
Custom Fields:
  - is_service_product: true
```

### In ERPNext (Required)

Create matching items in ERPNext:

1. Go to **Stock > Item > New**
2. Create items:

#### SERVICE-TEL-AVIS
```
Item Code: SERVICE-TEL-AVIS
Item Name: Service: Telefonisches Avis
Item Group: Services
Is Stock Item: No
Standard Rate: 7.50
```

#### SERVICE-FORKLIFT
```
Item Code: SERVICE-FORKLIFT
Item Name: Service: Hebebühne / Forklift
Item Group: Services
Is Stock Item: No
Standard Rate: 0 (or your desired rate)
```

---

## 4. Field Mapping Reference

The integration supports multiple field name variations for flexibility:

| Field Type | Accepted Shopware Field Names |
|------------|------------------------------|
| PO Number | `custom_po_number`, `customer_reference`, `po_number`, `poNumber` |
| Tel. Avis | `custom_tel_avis`, `tel_avis`, `telAvis`, `telephone_notification` |
| Forklift | `custom_forklift_required`, `forklift_required`, `forkliftRequired`, `hebebuehne` |
| Invoice Email | `invoice_email`, `invoiceEmail`, `billing_email`, `billingEmail` |

---

## 5. Invoice Email / Billing Address Note

The `invoice_email` field is mapped to the **Customer** document in ERPNext, not the Sales Order. This allows:

1. **Persistent storage**: The alternative invoice email is saved on the customer record
2. **Reuse**: Future orders from the same customer can use this email
3. **Flexibility**: You can manually update it in ERPNext if needed

### How it works:

1. Customer checks out with `invoice_email` = "buchhaltung@firma.de"
2. ERPNext syncs the order and updates the Customer's `invoice_email` field
3. When generating invoices, you can use this field in your print formats/email templates

### Print Format Usage:

```html
{% if doc.customer %}
  {% set invoice_email = frappe.db.get_value("Customer", doc.customer, "invoice_email") %}
  {% if invoice_email %}
    <p>Invoice Email: {{ invoice_email }}</p>
  {% endif %}
{% endif %}
```

---

## 6. Webhooks (Optional but Recommended)

For real-time order sync, configure webhooks in Shopware:

1. Go to **Settings > Extensions > Webhooks**
2. Add webhooks:

### Order Placed
```
Name: ERPNext Order Sync
URL: https://your-erpnext.domain/api/method/ecommerce_integrations.shopware6.connection.webhook_handler
Event: order.placed
```

### Payment Status Changed
```
Name: ERPNext Payment Update
URL: https://your-erpnext.domain/api/method/ecommerce_integrations.shopware6.connection.webhook_handler
Event: order_transaction.paid
```

---

## 7. Troubleshooting

### Custom fields not syncing?

1. Check that field names match the expected names (see Field Mapping Reference)
2. Verify the custom field set is assigned to the Order entity
3. Check Shopware Logs in ERPNext for sync errors

### Service items not being added?

1. Verify the items exist in ERPNext with the correct Item Code
2. Check ERPNext Shopware Settings > Checkout Service Products
3. Enable the service and set the correct Item link

### Invoice email not saving?

1. Ensure the `invoice_email` custom field exists on Customer DocType in ERPNext
2. Run `bench migrate` after installing/updating the integration

---

## Support

For issues with the integration, check:
1. **ERPNext Logs**: Error Log, Shopware Log
2. **Shopware Logs**: `var/log/` in your Shopware installation
3. **API Testing**: Use Postman or curl to test the webhook endpoint
