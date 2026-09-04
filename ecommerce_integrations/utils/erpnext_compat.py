"""Compatibility shim for ERPNext helper functions that moved location
partway through the v16 line.

ERPNext relocated several transaction-mapper helpers out of
``sales_order.py`` / ``sales_invoice.py`` / ``accounts_controller.py``
into dedicated ``mapper`` / ``services`` modules. Sites on an older v16
point release (before the relocation) only have the old path; sites on
a newer point release only have the new one. The app's dependency pin
(``erpnext>=16.0.0,<17.0.0``) spans both, so each helper is imported
defensively here — new location first, old as fallback — instead of
directly in the upstream-owned Shopify/Unicommerce/Zenoti modules,
so ``bench migrate`` doesn't crash on whichever side of the relocation
a given site happens to be on.
"""

try:
	from erpnext.selling.doctype.sales_order.mapper import make_delivery_note
except ImportError:
	from erpnext.selling.doctype.sales_order.sales_order import make_delivery_note

try:
	from erpnext.selling.doctype.sales_order.mapper import make_sales_invoice
except ImportError:
	from erpnext.selling.doctype.sales_order.sales_order import make_sales_invoice

try:
	from erpnext.accounts.doctype.sales_invoice.mapper import make_sales_return
except ImportError:
	from erpnext.accounts.doctype.sales_invoice.sales_invoice import make_sales_return

try:
	from erpnext.accounts.services.child_item_update import update_child_qty_rate
except ImportError:
	from erpnext.controllers.accounts_controller import update_child_qty_rate

try:
	from erpnext.accounts.services.taxes import add_taxes_from_tax_template
except ImportError:
	from erpnext.controllers.accounts_controller import add_taxes_from_tax_template

__all__ = [
	"make_delivery_note",
	"make_sales_invoice",
	"make_sales_return",
	"update_child_qty_rate",
	"add_taxes_from_tax_template",
]
