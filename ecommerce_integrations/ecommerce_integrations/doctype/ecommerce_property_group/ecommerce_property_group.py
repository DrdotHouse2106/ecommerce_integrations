"""Ecommerce Property Group — global catalog for ecommerce-side properties.

A Property Group is the single source of truth for a property's
display order, type ("Property" / "Text" / "Custom Field"), filterable
flag and per-backend sync toggles. Items reference a Group by its
``property_name`` (verbatim match to the legacy
``Item Ecommerce Property.property_name`` strings) through their
``Item Ecommerce Property.property_group`` link.

Why a single catalog: Shopware supports exactly one
``property_group.position`` per group — the storefront filter sidebar
is sorted by that one global field, no per-item override exists. The
old per-item flags (``sync_to_shopware`` / ``sync_to_medusa`` /
``property_type``) ended up reflecting that constraint anyway (the
same property name almost always carried identical flags across
items), so hoisting them out of the row dedups the data and lets the
operator change one knob to retune the storefront.

Hashing impact: the canonical's ``properties.ecommerce_properties``
section sorts by ``(group.display_order, group.name,
option.display_order, value)``. Changing a Group's ``display_order``
(or an Option's) flips the hash on every Item that references it, so
the next sync naturally re-pushes the new positions to Shopware and
Medusa metadata. Items whose own data didn't change re-emit the same
canonical and stay noop.
"""

from frappe.model.document import Document


class EcommercePropertyGroup(Document):
	pass
