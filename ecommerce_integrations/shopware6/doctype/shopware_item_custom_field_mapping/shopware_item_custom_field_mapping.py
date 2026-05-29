"""Shopware Item Custom Field Mapping child DocType.

Generic mapping from an Item DocType field (or Custom Field) to a
Shopware product ``customFields`` slot. The product-sync engine
reads the rows attached to Shopware Setting and emits the
configured key/value pairs into the ``customFields`` JSON on every
push. Operators add new flags (Idealo feed inclusion, Google
Shopping inclusion, manufacturer-recommended-age, …) without code
changes — create the Item Custom Field via Customize Form, add a
matching row here, and the new sync run already carries it.

The engine respects the ``field_type`` for coercion + the
``Skip-If-Empty`` mode to omit a key entirely when the source
value is empty (lets the Shopware default for the slot apply).
"""

from frappe.model.document import Document


class ShopwareItemCustomFieldMapping(Document):
    pass
