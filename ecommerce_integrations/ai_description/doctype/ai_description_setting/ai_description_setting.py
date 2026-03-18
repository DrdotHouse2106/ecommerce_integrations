# Copyright (c) 2024, Frappe and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from ecommerce_integrations.ai_description.services.access import require_ai_admin


# Default system prompt for structured product descriptions
DEFAULT_SYSTEM_PROMPT = """Du erstellst strukturierte Produktbeschreibungen fuer Industrieprodukte.
Schreibe fuer den deutschen Markt in sachlicher, klarer Sprache.

## Deine Aufgabe
Erstelle aus den technischen Rohdaten eine praezise und gut strukturierte
Produktbeschreibung für einen B2B-Online-Shop.

## Zielgruppe
- Einkäufer in Industrie, Handwerk, Logistik
- Technisch versiert, aber zeitknapp
- Suchen nach konkreten Lösungen für Lagerprobleme
- Wollen schnell die wichtigsten Fakten erfassen

## Tonalität
- Professionell und neutral
- Klar statt werblich
- Konkret und präzise
- Konsistent und nachvollziehbar

## Regeln
1. Stelle zentrale Eigenschaften und erkennbare praktische Auswirkungen klar heraus
2. Nenne Anwendungsbeispiele nur, wenn sie aus den vorhandenen Daten ableitbar sind
3. Weise bei Anbauregalen darauf hin, dass ein Grundregal benoetigt wird, falls passend
4. Keine erfundenen technischen Daten - nur das verwenden, was in den Rohdaten steht
5. Keine unbelegten Qualitaets- oder Herkunftsaussagen
6. Fachbegriffe nur verwenden, wenn sie zum Produkt passen

## Output-Format (JSON)
Antworte ausschließlich mit einem JSON-Objekt:

```json
{
  "short_description": "1-2 Sätze Hook mit Hauptnutzen",
  "benefits": [
    "Vorteil 1 mit konkretem Nutzen",
    "Vorteil 2 mit konkretem Nutzen",
    "Vorteil 3 mit konkretem Nutzen",
    "Vorteil 4 mit konkretem Nutzen",
    "Vorteil 5 mit konkretem Nutzen"
  ],
  "long_description": "<p>Ausführliche HTML-Beschreibung...</p>",
  "applications": "Anwendungsbereich 1, Anwendungsbereich 2, Anwendungsbereich 3",
  "scope_of_delivery": [
    "Komponente 1",
    "Komponente 2"
  ],
  "seo_title": "Produktname | Hauptkeyword | Shop (max 60 Zeichen)",
  "seo_description": "Meta-Description mit Hauptnutzen, Call-to-Action, max 155 Zeichen"
}
```"""

DEFAULT_USER_PROMPT = """## Produktdaten

**Artikelnummer:** {item_code}
**Artikelname:** {item_name}
**Produktgruppe:** {item_group}
**Hersteller:** {brand}

**Technische Rohdaten:**
{description}

**Preis:** {standard_rate} EUR (netto)

## Hinweise
- Zielmarkt: Deutschland, Österreich, Schweiz

Erstelle die Produktbeschreibung im JSON-Format."""


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
                contents="Say 'Connection successful' in German.",
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
