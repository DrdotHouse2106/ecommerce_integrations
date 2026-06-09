"""Ecommerce Configurator Settings — singleton that drives the per-item
``is_configurable`` flag emitted into Medusa metadata and Shopware
customFields.

The flag tells the storefront PDP whether to render the
"Konfigurieren" CTA badge. Running the full resolver per pageview
would be too expensive, so the engine pre-computes the boolean from
operator-configured rules:

1. Optional explicit override on ``Item.is_configurable`` (a Check
   custom field the operator adds via Customize Form if they want
   row-level control). When ``Respect Item Override`` is on and the
   field is set to 1, that wins.
2. Otherwise the item is eligible when its ``item_group`` (or any
   ancestor when ``include_descendants`` is on for that row) appears
   in ``eligible_item_groups``.
3. Exclusion keywords veto: if any keyword from ``exclusion_keywords``
   appears (case-insensitive substring) in ``item_name``, the item
   is forced to ``false`` regardless of inclusion rules.

When ``enabled`` is off, the engine emits ``false`` for every item —
useful for staging the storefront resolver before showing the badge.

Why a Setting instead of a Patch: operators can flip the master
toggle, add new item groups, or change the backend key names without
a code change or migration. The product-sync engine reads this
singleton once per job (memoised on ``frappe.local``); a change here
flips ``basic.is_configurable`` in the canonical → hash flip → next
sync re-pushes affected items.
"""

from frappe.model.document import Document


class EcommerceConfiguratorSettings(Document):
	pass
