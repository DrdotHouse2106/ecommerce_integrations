"""Item Additional Group child DocType.

Backs ``Item.additional_item_groups`` (Table MultiSelect): lets an
operator assign an Item to Shopware categories beyond its single
native ``item_group``, without requiring the separate Webshop app's
``Website Item`` / ``Website Item Group`` tables.

Read by :func:`ecommerce_integrations.shopware6.export.category_handler.get_all_item_categories`
when building the category list for a product push.
"""

from frappe.model.document import Document


class ItemAdditionalGroup(Document):
    pass
