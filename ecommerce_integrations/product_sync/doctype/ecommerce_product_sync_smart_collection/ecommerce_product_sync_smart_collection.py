"""Child-table controller for ``Ecommerce Product Sync Smart Collection``.

The doctype just declares fields; no behaviour needs to live here.
Kept as a real Python module (rather than relying on the auto-generated
shim) so the controller import survives reload cycles in dev mode.
"""

from __future__ import annotations

from frappe.model.document import Document


class EcommerceProductSyncSmartCollection(Document):
    pass
