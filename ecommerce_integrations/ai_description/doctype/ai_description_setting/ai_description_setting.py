# Copyright (c) 2024, Frappe and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from ecommerce_integrations.ai_description.services.access import require_ai_admin

# Default system prompt for structured product descriptions
DEFAULT_SYSTEM_PROMPT = """You create structured product descriptions for an online shop.
Write in a professional, clear, and neutral tone.

## Your Task
Create a precise and well-structured product description from raw technical data
for a B2B online shop.

## Target Audience
- Professional buyers in industry, trade, and logistics
- Technically knowledgeable but time-constrained
- Looking for specific solutions
- Want to quickly grasp the key facts

## Tone
- Professional and neutral
- Clear, not promotional
- Specific and precise

## Rules
1. Clearly highlight key features and their practical implications
2. Only mention use cases that can be derived from the provided data
3. Do not invent technical specifications - only use what is in the raw data
4. No unsubstantiated quality or origin claims
5. Only use technical terms when they are relevant to the product

## Output Format (JSON)
Respond exclusively with a JSON object:

```json
{
  "short_description": "1-2 sentence hook with main benefit",
  "benefits": [
    "Benefit 1 with specific value",
    "Benefit 2 with specific value",
    "Benefit 3 with specific value",
    "Benefit 4 with specific value",
    "Benefit 5 with specific value"
  ],
  "long_description": "<p>Detailed HTML description...</p>",
  "applications": "Application area 1, Application area 2, Application area 3",
  "scope_of_delivery": [
    "Component 1",
    "Component 2"
  ],
  "seo_title": "Product Name | Main Keyword (max 60 characters)",
  "seo_description": "Meta description with main benefit, max 155 characters"
}
```"""

DEFAULT_USER_PROMPT = """## Product Data

**Item Code:** {item_code}
**Item Name:** {item_name}
**Product Group:** {item_group}
**Brand:** {brand}

**Technical Data:**
{description}

**Price:** {standard_rate}

Create the product description in JSON format."""


class AIDescriptionSetting(Document):
    def validate(self):
        if self.enabled and not self.gemini_api_key:
            frappe.throw(_("Google Gemini API Key is required when enabled"))

        if self.temperature and (self.temperature < 0 or self.temperature > 2):
            frappe.throw(_("Temperature must be between 0 and 2"))

        if self.batch_size and self.batch_size < 1:
            frappe.throw(_("Batch size must be at least 1"))

    def before_save(self):
        # Set default prompts if empty
        if not self.system_prompt:
            self.system_prompt = DEFAULT_SYSTEM_PROMPT

        if not self.user_prompt_template:
            self.user_prompt_template = DEFAULT_USER_PROMPT

    @frappe.whitelist()
    def test_connection(self):
        """Test Gemini API connection"""
        require_ai_admin()
        try:
            from ecommerce_integrations.ai_description.gemini import get_gemini_client

            client, model_name, generation_config = get_gemini_client()

            # Simple test prompt
            response = client.models.generate_content(
                model=model_name,
                contents="Say 'Connection successful'.",
                config=generation_config,
            )

            if response and response.text:
                return {
                    "success": True,
                    "message": f"Connection successful! Model: {self.gemini_model}",
                    "response": response.text
                }
            else:
                return {
                    "success": False,
                    "message": "No response from API"
                }
        except Exception as e:
            return {
                "success": False,
                "message": str(e)
            }

    @frappe.whitelist()
    def reset_prompts(self):
        """Reset prompts to defaults"""
        require_ai_admin()
        self.system_prompt = DEFAULT_SYSTEM_PROMPT
        self.user_prompt_template = DEFAULT_USER_PROMPT
        self.save()
        return {"success": True, "message": "Prompts reset to defaults"}

    @frappe.whitelist()
    def get_pending_items_count(self):
        """Get count of items pending AI description generation"""
        require_ai_admin()
        filters = self._build_item_filters()

        count = frappe.db.count("Item", filters=filters)

        return {
            "count": count,
            "filters": filters
        }

    def _build_item_filters(self):
        """Build filters for item selection"""
        filters = {
            "disabled": 0,
            "has_variants": 0
        }

        if self.only_items_without_description:
            filters["ai_description_generated"] = 0

        if self.only_sellable_items:
            filters["is_sales_item"] = 1

        if self.item_group_filter:
            # Include all child groups
            item_groups = get_item_groups_with_children(self.item_group_filter)
            filters["item_group"] = ["in", item_groups]

        return filters


def get_settings():
    """Get AI Description Settings singleton"""
    return frappe.get_single("AI Description Setting")


def get_item_groups_with_children(parent_group):
    """Get item group and all its children recursively"""
    groups = [parent_group]

    children = frappe.get_all(
        "Item Group",
        filters={"parent_item_group": parent_group},
        pluck="name"
    )

    for child in children:
        groups.extend(get_item_groups_with_children(child))

    return groups
