# Copyright (c) 2026, Frappe Technologies and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class EcommerceChannelOverride(Document):
    """Per-item override row for a single backend / sales channel.

    Lives as a child table on ``Item`` via the custom field
    ``ecommerce_channel_overrides`` (installed by
    :mod:`ecommerce_integrations.patches.add_item_ecommerce_channel_overrides`).

    The :func:`ecommerce_integrations.catalog_mirror.api.resolve_item_for_form`
    resolver gives these rows the highest priority — they override both
    Catalog Mirror node assignments and Smart Collection matches.
    """

    pass
