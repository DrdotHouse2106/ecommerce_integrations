# Customer comment as a fulfillment hint

## What it does

Buyers can leave a free-text comment on a storefront order ("please ring
twice", "side entrance", …). Storefront plugins also use that comment to
pass structured instructions, e.g. a pickup-at-event plugin that writes

```
ABHOLUNG: Oldtimertreffen Musterstadt, Musterstadt, 17.07.2027
```

at the start of the comment. This feature carries the comment from the
order through picking, packing and delivery and makes it impossible to
overlook.

## Fields

Installed on `bench migrate` (patch `add_ecommerce_customer_comment_fields`)
and on fresh installs (`install.after_install`). The "Fehlende Custom Fields
nachinstallieren" maintenance button re-runs the same setup.

| Doctype | Field | Type | Behaviour |
|---|---|---|---|
| Sales Order | `ecommerce_customer_comment` ("Kundenkommentar") | Small Text | Set by the order import. Editable, also after submit (`allow_on_submit`). |
| Sales Order | `ecommerce_comment_highlight` ("Hervorgehobener Hinweis") | Check, read-only | Set automatically when the comment starts with a highlight prefix. Available as a list filter. |
| Delivery Note, Pick List, Packing Slip | same two fields | read-only | Recomputed from the linked Sales Order(s) on every save of a draft and on submit. |

Why the Sales Order field is editable: the same hint is useful for manual or
phone orders, and staff must be able to correct a comment or add a pickup
instruction after the order was submitted. The storefront only overwrites it
when it sends a *different, non-empty* comment; an update without a comment
never clears what staff entered.

How the value reaches the downstream documents: through `validate` hooks
(`ecommerce_integrations/customer_comment.py`), not through ERPNext's
same-fieldname mapping. That way it also works for

- Pick Lists, which reference orders per location row,
- Delivery Notes / Pick Lists that combine several orders — each comment is
  then prefixed with its order name (`[SO-00001] …`),
- documents created outside the standard "Create" buttons (API, imports).

Packing Slips take the value of their Delivery Note.

A comment changed on the Sales Order after a Delivery Note / Pick List was
submitted is not pushed into the submitted document.

## Storage format

The comment is stored as plain text: HTML tags are removed, entities decoded,
control characters dropped, whitespace trimmed and the length capped at 2000
characters. Every renderer escapes the value again on output.

## Highlight prefixes

A comment is highlighted (red banner, red frame in print) when it starts —
case-insensitively, ignoring leading whitespace — with one of the configured
prefixes. Default: `ABHOLUNG:`.

Configure per site in `site_config.json`:

```json
{
  "ecommerce_comment_highlight_prefixes": ["ABHOLUNG:", "PICKUP:", "EXPRESS:"]
}
```

or `bench --site <your-site> set-config ecommerce_comment_highlight_prefixes '["PICKUP:"]' --parse`.
A comma-separated string works too. An empty list disables highlighting. The
flag is computed when the comment is saved, so after changing the prefixes,
existing documents pick up the new rule on their next save.

## Where it shows up

- **Forms** (Sales Order, Pick List, Delivery Note, Packing Slip): a banner at
  the top of the form. Yellow for a normal comment, red with larger text when
  highlighted.
- **Print format "Kommissionierliste"** (Pick List, shipped with this app,
  selectable — not set as default): header with pick list number, date,
  order(s) and customer, then the comment bold and large (red frame when
  highlighted), then the item table (item, description, warehouse, qty + UOM,
  batch / serial no., tick box). ERPNext has no storage-bin field, so the
  warehouse column is the finest location available. With several orders the
  order name is shown under each item.
- **Print format "Lieferschein Lager"** (Delivery Note, internal warehouse
  copy, selectable — not default): same layout for Delivery Notes.
- The customer-facing **"Versandbestätigung"** deliberately does *not* print
  the comment: the Sales Order field can contain internal notes added by
  staff.
- **Your own print formats**:
  import the shared macro

  ```jinja
  {%- from "ecommerce_integrations/templates/includes/customer_comment.html"
      import customer_comment_block with context -%}

  {{ customer_comment_block(doc) }}
  {# or with a caption: #}
  {{ customer_comment_block(doc, label=_("Note")) }}
  ```

  The macro escapes the value and renders nothing when the comment is empty.
  In ERPNext's standard (non-Jinja) print layout the field is printed like any
  other Small Text field unless you hide it.

## Channels

- **Shopware 6**: `order.customerComment` is imported when the order is
  created. On order update webhooks the comment is taken from the payload when
  present; otherwise the order is fetched once — skipped when the payload's
  `updatedFields` show that the comment did not change.
- **Medusa v2**: Medusa has no native order comment. If your storefront stores
  one in the order/cart `metadata`, add a checkout-field mapping (Medusa
  Setting → Checkout Fields) of type *Sales Order Field* with target
  `ecommerce_customer_comment`. The Sales Order `validate` hook normalises the
  value and sets the highlight flag.
- **Manual orders**: just type into the field.
