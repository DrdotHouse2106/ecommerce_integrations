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
        rules = response.get("data", [])

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


def ensure_all_channel_rules(client, setting) -> dict[str, str]:
    """
    Ensure all sales channels have corresponding Shopware rules.

    Creates rules for channels that don't have one yet and updates
    the shopware_rule_id field in the setting's sales_channels table.

    Args:
        client: Shopware API client
        setting: Shopware Setting document

    Returns:
        Dict mapping sales_channel_id to rule_id
    """
    rule_map = {}

    for channel in (setting.sales_channels or []):
        if not channel.active:
            continue

        # Check if rule already exists in channel
        if channel.shopware_rule_id:
            rule_map[channel.sales_channel_id] = channel.shopware_rule_id
            continue

        # Create rule for this channel
        rule_id = get_or_create_sales_channel_rule(
            client,
            channel.sales_channel_id,
            channel.sales_channel_name
        )

        if rule_id:
            rule_map[channel.sales_channel_id] = rule_id
            # Update the channel row
            channel.shopware_rule_id = rule_id

    return rule_map


def get_channel_rule_id(client, sales_channel_id: str, setting) -> str | None:
    """
    Get the Shopware rule ID for a specific sales channel.

    First checks the cached rule_id in the setting, then falls back
    to creating/fetching the rule.

    Args:
        client: Shopware API client
        sales_channel_id: Shopware sales channel UUID
        setting: Shopware Setting document

    Returns:
        Rule ID if found/created, None otherwise
    """
    # Find channel in setting
    for channel in (setting.sales_channels or []):
        if channel.sales_channel_id == sales_channel_id:
            # Use cached rule_id if available
            if channel.shopware_rule_id:
                return channel.shopware_rule_id

            # Create rule and cache it
            rule_id = get_or_create_sales_channel_rule(
                client,
                channel.sales_channel_id,
                channel.sales_channel_name
            )
            if rule_id:
                channel.shopware_rule_id = rule_id
            return rule_id

    return None


def delete_sales_channel_rule(client, rule_id: str) -> bool:
    """
    Delete a Shopware rule by ID.

    Args:
        client: Shopware API client
        rule_id: Shopware rule UUID

    Returns:
        True if successful, False otherwise
    """
    try:
        client.request_delete(f"rule/{rule_id}")
        return True
    except Exception as e:
        get_logger().error(f"Failed to delete rule {rule_id}: {e}", persist=False)
        return False
