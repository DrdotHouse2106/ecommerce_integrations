# Copyright (c) 2024, Frappe and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


# Default system prompt for B2B industrial products
DEFAULT_SYSTEM_PROMPT = """Du bist ein erfahrener B2B-Texter für Industrieprodukte, spezialisiert auf
Lagertechnik und Betriebseinrichtungen. Du schreibst für den deutschen Markt.

## Deine Aufgabe
Erstelle aus den technischen Rohdaten eine verkaufsstarke, SEO-optimierte
Produktbeschreibung für einen B2B-Online-Shop.

## Zielgruppe
- Einkäufer in Industrie, Handwerk, Logistik
- Technisch versiert, aber zeitknapp
- Suchen nach konkreten Lösungen für Lagerprobleme
- Wollen schnell die wichtigsten Fakten erfassen

## Tonalität
- Professionell, aber nicht steif
- Nutzenorientiert statt feature-fokussiert
- Konkret und präzise
- Vertrauenswürdig ({company_name})

## Regeln
1. Immer den NUTZEN vor dem FEATURE nennen ("800 kg Tragkraft für schwere Formen" statt "Tragkraft: 800 kg")
2. Konkrete Anwendungsbeispiele nennen (Werkzeugbau, Formenlager, etc.)
3. Bei Anbauregalen erwähnen, dass ein Grundregal benötigt wird
4. Deutsche Qualität und Zertifizierungen betonen
5. Keine erfundenen technischen Daten - nur das verwenden, was in den Rohdaten steht
6. SEO-Keywords natürlich einbauen

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

Erstelle die optimierte Produktbeschreibung im JSON-Format."""


class AIDescriptionSetting(Document):
    def validate(self):
        if self.enabled and not self.gemini_api_key:
            frappe.throw("Google Gemini API Key is required when enabled")

        if self.temperature and (self.temperature < 0 or self.temperature > 2):
            frappe.throw("Temperature must be between 0 and 2")

        if self.batch_size and self.batch_size < 1:
            frappe.throw("Batch size must be at least 1")

    def before_save(self):
        # Set default prompts if empty
        if not self.system_prompt:
            self.system_prompt = DEFAULT_SYSTEM_PROMPT

        if not self.user_prompt_template:
            self.user_prompt_template = DEFAULT_USER_PROMPT

    @frappe.whitelist()
    def test_connection(self):
        """Test Gemini API connection"""
        try:
            from ..gemini import get_gemini_client

            model = get_gemini_client()

            # Simple test prompt
            response = model.generate_content("Say 'Connection successful' in German.")

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
        self.system_prompt = DEFAULT_SYSTEM_PROMPT
        self.user_prompt_template = DEFAULT_USER_PROMPT
        self.save()
        return {"success": True, "message": "Prompts reset to defaults"}

    @frappe.whitelist()
    def get_pending_items_count(self):
        """Get count of items pending AI description generation"""
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
