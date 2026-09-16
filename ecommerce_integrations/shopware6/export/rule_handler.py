"""
Shopware 6 Rule Handler

Manages Shopware Rules for sales-channel-specific pricing.
Creates rules with condition "Sales Channel = X" that are then used
to attach channel-specific prices to products.
"""


import frappe

from ecommerce_integrations.shopware6.base.cache_manager import get_cache
from ecommerce_integrations.shopware6.export.utils import generate_uuid
from ecommerce_integrations.shopware6.utils import get_logger


def get_or_create_sales_channel_rule(
    client,
    sales_channel_id: str,
    sales_channel_name: str
) -> str | None:
    """
    Get existing or create new Shopware Rule for a sales channel.

    The rule has the condition: "Sales Channel equals [channel_id]"
    This rule is used to attach channel-specific prices to products.

    Args:
        client: Shopware API client
        sales_channel_id: Shopware sales channel UUID
        sales_channel_name: Human-readable name for the rule

    Returns:
        Shopware Rule ID if successful, None otherwise
    """
    cache = get_cache()
    cache_key = f"sc_rule_{sales_channel_id}"

    # Check cache first
    cached_id = cache.get("rule", cache_key)
    if cached_id:
        return cached_id

    try:
        # Search for existing rule by name pattern
        rule_name = f"SC Price: {sales_channel_name}"
        response = client.request_post(
            "search/rule",
            {
                "filter": [
                    {"type": "equals", "field": "name", "value": rule_name}
                ],
                "limit": 1
            }
        )
        rules = response.data or []

        if rules:
            rule_id = rules[0]["id"]
            cache.set("rule", cache_key, rule_id)
            return rule_id

        # Create new rule with sales channel condition
        rule_id = generate_uuid(f"rule_sc_{sales_channel_id}")

        rule_payload = {
            "id": rule_id,
            "name": rule_name,
            "priority": 100,
            "description": f"Price rule for sales channel: {sales_channel_name}",
            "conditions": [
                {
                    "type": "orContainer",
                    "children": [
                        {
                            "type": "salesChannel",
                            "value": {
                                "salesChannelIds": [sales_channel_id],
                                "operator": "="
                            }
                        }
                    ]
                }
            ]
        }

        client.request_post("rule", rule_payload)
        cache.set("rule", cache_key, rule_id)

        frappe.logger("shopware6").info(
            f"Created Shopware rule '{rule_name}' for sales channel {sales_channel_name}"
        )

        return rule_id

    except Exception:
        get_logger().error("Error occurred", persist=False)
        return None


