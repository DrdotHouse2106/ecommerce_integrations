"""Install the customer-comment fulfillment fields on existing sites.

Adds ``ecommerce_customer_comment`` / ``ecommerce_comment_highlight`` to
Sales Order, Delivery Note, Pick List and Packing Slip (see
``ecommerce_integrations/customer_comment.py``). Fresh installs get the same
fields through ``install.after_install``.

Idempotent: ``create_custom_fields(..., update=True)`` upserts by fieldname,
and doctypes missing on a minimal site are skipped.
"""


def execute() -> None:
	from ecommerce_integrations.ecommerce_integrations.customer_comment import setup_custom_fields

	setup_custom_fields()
