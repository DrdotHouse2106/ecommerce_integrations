# Repository rules

This is a public Frappe app fork. Anything pushed lands on a public GitHub repository.

## Neutrality rule (hard requirement)

The codebase, commit messages, branch names, fixtures, tests, docs, scripts and history must stay 100% neutral. Treat this as load-bearing.

Forbidden in any file, any commit message, any branch name:

- Real company names, brand names, product line names, customer names, employee names
- Real customer email addresses, real phone numbers, real postal addresses
- Real domains owned by the operator (anything beyond `example.com` / `yourshop.com` placeholders)
- Real ERPNext site names, bench paths tied to a specific install (`bench --site <real-site>`, `/home/<user>/...`)
- Real item codes, SKU codes, customer codes, sales channel UUIDs, order numbers from a production system
- Hardcoded VAT IDs, IBANs, BICs, tax numbers
- Local debugging notes, "this works because of our setup", incident-specific docstrings

If you need to refer to something concrete, use placeholders: `example.com`, `yourshop.com`, `your-site`, `CUST-001`, `Demo Kunde GmbH`, `ITEM-001`.

## What this means in practice

- **Before committing**, grep the diff for the operator's known company names and domains. If any survive the cleanup pass, fix them before the commit.
- **No one-off scripts at repo root or in modules.** Cleanup snippets, debug helpers, one-time data migrations from a private install do not belong in the plugin. Keep them in the operator's local working tree, never `git add` them.
- **Patches in `ecommerce_integrations/patches/`** must be safe to run on any install. They must no-op gracefully if the install does not match (`if not frappe.db.exists(...): return`). They must not reference doctypes/fields/notification names that only exist on the operator's site.
- **Fixtures** in `ecommerce_integrations/fixtures/` ship to every install. CC recipients, sender emails, and signature lines must come from the user's `Ecommerce Channel Branding` doctype, not be hardcoded in JSON.
- **Test data** uses generic placeholders (`Test GmbH`, `Demo Kunde`, `Musterstraße`, `DE123456789`).
- **Print formats and email templates** read all branding (logo, address, IBAN, sender name, support email, imprint) from `Ecommerce Channel Branding`, never inline.
- **Commit messages** describe the change generically. Don't reference the originating incident, the customer who reported it, or internal ticket IDs.
- **Branch names** describe the feature, not the operator (`feat/multi-channel-integrations`, not `example-cleanup`).

## Why

This fork exists publicly so others can install it. Any private detail that leaks in — even in deleted files, even in old commit history — is permanent on GitHub once pushed. History rewrites and force-pushes are expensive and don't help if a clone or a fork already grabbed the leaked data.

The cheap fix is at write time. Get it right before staging.
