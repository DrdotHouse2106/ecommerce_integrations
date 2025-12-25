# Copyright (c) 2024, Frappe and contributors
# For license information, please see license.txt

"""
Google Gemini API Integration for Product Description Generation

Supports:
- gemini-2.5-flash (stable, default)
- gemini-2.5-flash-lite (cost-effective, recommended for bulk)
- gemini-3-flash-preview (latest, fastest - Dec 2025)

Features:
- Single item generation
- Multi-item batch generation (multiple products per API call for efficiency)
"""

import frappe
import json
import re
import time
from typing import Optional, List, Dict, Any


def get_settings():
    """Get AI Description Settings singleton"""
    return frappe.get_single("AI Description Setting")


def get_gemini_client(max_tokens_override: int = None):
    """
    Get configured Gemini GenerativeModel client

    Args:
        max_tokens_override: Override max_tokens setting (for batch processing)

    Returns:
        genai.GenerativeModel: Configured model instance
    """
    try:
        import google.generativeai as genai
    except ImportError:
        frappe.throw(
            "google-generativeai package not installed. "
            "Install with: pip install google-generativeai"
        )

    settings = get_settings()

    if not settings.enabled:
        frappe.throw("AI Description Generation is not enabled")

    if not settings.gemini_api_key:
        frappe.throw("Gemini API Key not configured")

    api_key = settings.get_password("gemini_api_key")
    genai.configure(api_key=api_key)

    # Model name mapping
    model_map = {
        "gemini-2.5-flash": "gemini-2.5-flash",
        "gemini-2.5-flash-lite": "gemini-2.5-flash-lite",
        "gemini-3-flash-preview": "gemini-3-flash-preview"
    }

    model_name = model_map.get(settings.gemini_model, "gemini-2.5-flash")

    # Use override or settings value
    max_tokens = max_tokens_override or int(settings.max_tokens or 8192)

    # Configure generation settings
    generation_config = {
        "temperature": float(settings.temperature or 0.7),
        "max_output_tokens": max_tokens,
        "response_mime_type": "application/json"
    }

    model = genai.GenerativeModel(
        model_name=model_name,
        generation_config=generation_config
    )

    return model


def generate_description(item_code: str) -> dict:
    """
    Generate AI description for a single item

    Args:
        item_code: ERPNext Item code

    Returns:
        dict with generated content or error
    """
    settings = get_settings()
    start_time = time.time()

    # Get item data
    item = frappe.get_doc("Item", item_code)

    if not item:
        return {"success": False, "error": f"Item {item_code} not found"}

    # Build prompts
    system_prompt = build_system_prompt(settings)
    user_prompt = build_user_prompt(settings, item)

    # Create log entry
    log = frappe.new_doc("AI Description Log")
    log.item = item_code
    log.status = "Pending"
    log.model_used = settings.gemini_model
    log.prompt_used = f"SYSTEM:\n{system_prompt}\n\nUSER:\n{user_prompt}"
    log.insert(ignore_permissions=True)

    try:
        model = get_gemini_client()

        # Combine prompts for Gemini (it doesn't have separate system prompt in basic API)
        full_prompt = f"{system_prompt}\n\n---\n\n{user_prompt}"

        response = model.generate_content(full_prompt)

        if not response or not response.text:
            raise Exception("Empty response from Gemini API")

        # Parse JSON response
        result = parse_ai_response(response.text)

        generation_time = time.time() - start_time

        # Update log
        log.status = "Success"
        log.generation_time = generation_time
        log.response_raw = response.text

        # Extract token count if available
        if hasattr(response, 'usage_metadata'):
            log.tokens_used = response.usage_metadata.total_token_count

        log.save(ignore_permissions=True)

        # Update item with generated content
        update_item_with_description(item, result, settings)

        return {
            "success": True,
            "item_code": item_code,
            "generation_time": generation_time,
            "result": result
        }

    except Exception as e:
        generation_time = time.time() - start_time
        error_msg = str(e)

        log.status = "Failed"
        log.generation_time = generation_time
        log.error_message = error_msg
        log.save(ignore_permissions=True)

        frappe.log_error(
            title=f"AI Description Generation Failed: {item_code}",
            message=error_msg
        )

        return {
            "success": False,
            "item_code": item_code,
            "error": error_msg
        }


def build_system_prompt(settings, is_batch: bool = False) -> str:
    """Build system prompt with company name substitution"""
    if is_batch:
        return build_batch_system_prompt(settings)

    prompt = settings.system_prompt or ""

    # Get company name from default company or first company
    company_name = frappe.defaults.get_user_default("company")
    if not company_name:
        companies = frappe.get_all("Company", limit=1, pluck="name")
        company_name = companies[0] if companies else "Unser Unternehmen"

    prompt = prompt.replace("{company_name}", company_name)

    return prompt


def build_batch_system_prompt(settings) -> str:
    """Build system prompt for multi-product batch processing"""
    return '''Du bist ein erfahrener B2B-Texter für Industrieprodukte, spezialisiert auf
Lagertechnik und Betriebseinrichtungen. Du schreibst für den deutschen Markt.

## Shop-Name
WICHTIG: Verwende IMMER "Example.Shop" als Shop-Namen (nicht "Example GmbH").

## Deine Aufgabe
Erstelle aus den technischen Rohdaten verkaufsstarke, SEO-optimierte
Produktbeschreibungen für einen B2B-Online-Shop.

Du erhältst MEHRERE PRODUKTE auf einmal und musst für JEDES Produkt eine eigene Beschreibung erstellen.

## Zielgruppe
- Einkäufer in Industrie, Handwerk, Logistik
- Technisch versiert, aber zeitknapp
- Suchen nach konkreten Lösungen für Lagerprobleme

## Tonalität
- Professionell, aber nicht steif
- Nutzenorientiert statt feature-fokussiert
- Konkret und präzise

## Regeln
1. Immer den NUTZEN vor dem FEATURE nennen
2. Konkrete Anwendungsbeispiele nennen (Werkzeugbau, Formenlager, etc.)
3. Bei Anbauregalen erwähnen, dass ein Grundregal benötigt wird
4. Deutsche Qualität und Zertifizierungen betonen
5. Keine erfundenen technischen Daten - nur das verwenden, was in den Rohdaten steht
6. SEO-Keywords natürlich einbauen

## WICHTIGE SEO-Regeln (STRIKT einhalten!)
7. seo_title: EXAKT 50-58 Zeichen, Format: "Kurzname | Keyword | Example.Shop"
8. seo_description: EXAKT 130-155 Zeichen, rein informativer Satz
   - VERBOTEN: "Jetzt", "bei Example", "im Example.Shop", "bestellen", "kaufen"
   - Schreibe NUR über Produkteigenschaften und Vorteile
   - ✓ GUT: "Schwerlast-Palettenregal für 3.000 kg pro Ebene. Pulverbeschichteter Stahl, TÜV-geprüft. Ideal für Industrie und Logistik."
   - ✓ GUT: "Kompakter Auflagedeckel aus robustem PP. Passt auf Euro-Stapelkästen 300x400mm. Schützt zuverlässig vor Staub und Schmutz."
   - ✗ SCHLECHT: "...Jetzt im Example.Shop." oder "...bei Example.Shop bestellen."

## HTML-Formatierung in long_description (WICHTIG!)
Nutze reichhaltige HTML-Formatierung für bessere Lesbarkeit:
- <strong>...</strong> für wichtige Begriffe (Produktname, Maße, Tragkraft, Hersteller)
- <p>...</p> für Absätze (mind. 3-4 Absätze)
- <ul><li>...</li></ul> für Aufzählungen (z.B. Lieferumfang, Features)
- <table>...</table> für technische Daten wenn sinnvoll (Maße, Gewicht, Material)
- Hebe 3-5 Schlüsselbegriffe pro Beschreibung mit <strong> hervor
- Strukturiere den Text visuell ansprechend

## Länge der long_description
- Ziel: 200-350 Wörter (ausführlich aber substanziell)
- Vermeide Marketing-Floskeln und leere Phrasen
- Fokussiere auf: Anwendungsszenarien, Vorteile, technische Details, Kompatibilität
- Schreibe so, dass ein Einkäufer alle wichtigen Infos bekommt

## Output-Format (JSON Array)
Antworte mit einem JSON-Array. Für JEDES Produkt ein Objekt mit dem item_code als Schlüssel:

{
  "products": [
    {
      "item_code": "ARTIKEL-123",
      "short_description": "1-2 Sätze Hook mit Hauptnutzen",
      "benefits": ["Vorteil 1", "Vorteil 2", "Vorteil 3", "Vorteil 4", "Vorteil 5"],
      "long_description": "<p>Ausführliche HTML-Beschreibung...</p>",
      "applications": "Anwendungsbereich 1, Anwendungsbereich 2",
      "scope_of_delivery": ["Komponente 1", "Komponente 2"],
      "seo_title": "Produktname | Keyword | Example.Shop",
      "seo_description": "Meta-Description unter 155 Zeichen."
    },
    {
      "item_code": "ARTIKEL-456",
      ...
    }
  ]
}'''


def build_user_prompt(settings, item) -> str:
    """Build user prompt from template with item data"""
    template = settings.user_prompt_template or ""

    # Get item fields
    replacements = {
        "{item_code}": item.item_code or "",
        "{item_name}": item.item_name or "",
        "{item_group}": item.item_group or "",
        "{brand}": item.brand or "",
        "{description}": frappe.utils.strip_html(item.description or ""),
        "{standard_rate}": str(item.standard_rate or 0)
    }

    for placeholder, value in replacements.items():
        template = template.replace(placeholder, value)

    return template


def build_batch_user_prompt(items: List[Dict]) -> str:
    """Build user prompt for multiple products"""
    prompt_parts = ["## Produktdaten\n\nErstelle Beschreibungen für folgende Produkte:\n"]

    for i, item in enumerate(items, 1):
        prompt_parts.append(f"""
---
### Produkt {i}: {item.get('item_code', 'N/A')}

**Artikelnummer:** {item.get('item_code', '')}
**Artikelname:** {item.get('item_name', '')}
**Produktgruppe:** {item.get('item_group', '')}
**Hersteller:** {item.get('brand', '')}
**Technische Rohdaten:** {item.get('description', '')}
**Preis:** {item.get('standard_rate', 0)} EUR (netto)
""")

    prompt_parts.append("\n---\n\nErstelle für ALLE oben genannten Produkte die optimierten Beschreibungen im JSON-Format.")

    return "".join(prompt_parts)


def parse_ai_response(response_text: str) -> dict:
    """
    Parse AI response, extracting JSON from various formats

    Args:
        response_text: Raw response from Gemini

    Returns:
        dict with parsed content
    """
    # Try direct JSON parse first
    try:
        return json.loads(response_text)
    except json.JSONDecodeError:
        pass

    # Try to extract JSON from markdown code blocks
    json_match = re.search(r'```json\s*([\s\S]*?)\s*```', response_text)
    if json_match:
        try:
            return json.loads(json_match.group(1))
        except json.JSONDecodeError:
            pass

    # Try to extract JSON from any code blocks
    code_match = re.search(r'```\s*([\s\S]*?)\s*```', response_text)
    if code_match:
        try:
            return json.loads(code_match.group(1))
        except json.JSONDecodeError:
            pass

    # Try to find JSON object in text
    json_obj_match = re.search(r'\{[\s\S]*\}', response_text)
    if json_obj_match:
        try:
            return json.loads(json_obj_match.group(0))
        except json.JSONDecodeError:
            pass

    # If all parsing fails, return structured error
    return {
        "error": "Could not parse AI response",
        "raw_response": response_text[:1000]  # Truncate for storage
    }


def update_item_with_description(item, result: dict, settings):
    """
    Update Item document with generated description content

    Args:
        item: Item document or item_code string
        result: Parsed AI response
        settings: AI Description Settings
    """
    if "error" in result:
        frappe.logger().error(f"AI Description: Result contains error, not updating item")
        return False

    # Handle both document and item_code
    if isinstance(item, str):
        item_code = item
        item = frappe.get_doc("Item", item_code)
    else:
        item_code = item.item_code

    frappe.logger().info(f"AI Description: Updating item {item_code} with AI results")

    # Update fields
    if result.get("short_description"):
        item.db_set("ai_short_description", result["short_description"])

    if result.get("benefits"):
        benefits_json = json.dumps(result["benefits"], ensure_ascii=False) if isinstance(result["benefits"], list) else result["benefits"]
        item.db_set("ai_benefits", benefits_json)

    if result.get("long_description"):
        item.db_set("ai_long_description", result["long_description"])

    if result.get("applications"):
        item.db_set("ai_applications", result["applications"])

    if result.get("scope_of_delivery"):
        delivery_json = json.dumps(result["scope_of_delivery"], ensure_ascii=False) if isinstance(result["scope_of_delivery"], list) else result["scope_of_delivery"]
        item.db_set("ai_delivery_scope", delivery_json)

    if settings.include_seo:
        if result.get("seo_title"):
            item.db_set("ai_seo_title", result["seo_title"])  # No truncation - field is now TEXT
        if result.get("seo_description"):
            item.db_set("ai_seo_description", result["seo_description"][:160])

    # Set metadata
    now = frappe.utils.now()
    item.db_set("ai_description_generated", 1)
    item.db_set("ai_generation_date", now)
    item.db_set("ai_model_used", settings.gemini_model)

    return True


def generate_descriptions_batch(item_codes: list) -> dict:
    """
    Generate descriptions for multiple items (legacy - one API call per item)

    Args:
        item_codes: List of item codes

    Returns:
        dict with success/failure counts
    """
    results = {
        "total": len(item_codes),
        "success": 0,
        "failed": 0,
        "items": []
    }

    for item_code in item_codes:
        result = generate_description(item_code)

        if result.get("success"):
            results["success"] += 1
        else:
            results["failed"] += 1

        results["items"].append(result)

        # Commit after each item to prevent long transactions
        frappe.db.commit()

    return results


def generate_descriptions_multi_batch(item_codes: list, products_per_request: int = 20) -> dict:
    """
    Generate descriptions for multiple items using multi-product API calls.

    This is much more efficient than single-item calls:
    - System prompt is only sent once per batch
    - Reduces API call overhead
    - ~10x faster than single-item processing

    Args:
        item_codes: List of item codes to process
        products_per_request: Number of products per API call (default: 20, max recommended: 30)

    Returns:
        dict with processing results
    """
    settings = get_settings()
    start_time = time.time()

    # Limit products per request to avoid token limits
    products_per_request = min(products_per_request, 30)

    results = {
        "total": len(item_codes),
        "success": 0,
        "failed": 0,
        "batches_processed": 0,
        "items": [],
        "errors": []
    }

    # Split into batches
    batches = [item_codes[i:i + products_per_request] for i in range(0, len(item_codes), products_per_request)]
    total_batches = len(batches)

    frappe.logger().info(f"AI Multi-Batch: Processing {len(item_codes)} items in {total_batches} batches of up to {products_per_request}")

    for batch_num, batch in enumerate(batches, 1):
        batch_start = time.time()

        try:
            batch_result = _process_multi_batch(batch, settings, batch_num, total_batches)

            results["success"] += batch_result["success"]
            results["failed"] += batch_result["failed"]
            results["batches_processed"] += 1
            results["items"].extend(batch_result.get("items", []))

            if batch_result.get("errors"):
                results["errors"].extend(batch_result["errors"])

            batch_time = time.time() - batch_start
            frappe.logger().info(f"AI Multi-Batch: Batch {batch_num}/{total_batches} completed in {batch_time:.1f}s - {batch_result['success']} success, {batch_result['failed']} failed")

            # Commit after each batch
            frappe.db.commit()

            # Progress update
            frappe.publish_realtime(
                "ai_description_progress",
                {
                    "batch": batch_num,
                    "total_batches": total_batches,
                    "items_processed": results["success"] + results["failed"],
                    "total_items": len(item_codes)
                },
                user=frappe.session.user
            )

        except Exception as e:
            error_msg = f"Batch {batch_num} failed: {str(e)}"
            frappe.logger().error(f"AI Multi-Batch: {error_msg}")
            results["errors"].append(error_msg)
            results["failed"] += len(batch)

            # Log failed items
            for item_code in batch:
                results["items"].append({
                    "item_code": item_code,
                    "success": False,
                    "error": error_msg
                })

    total_time = time.time() - start_time
    results["total_time"] = total_time
    results["avg_time_per_item"] = total_time / len(item_codes) if item_codes else 0

    frappe.logger().info(f"AI Multi-Batch: Completed {len(item_codes)} items in {total_time:.1f}s ({results['avg_time_per_item']:.2f}s/item)")

    return results


def _process_multi_batch(item_codes: list, settings, batch_num: int, total_batches: int) -> dict:
    """
    Process a single batch of multiple items in one API call.

    Args:
        item_codes: List of item codes in this batch
        settings: AI Description Settings
        batch_num: Current batch number (for logging)
        total_batches: Total number of batches

    Returns:
        dict with batch results
    """
    result = {
        "success": 0,
        "failed": 0,
        "items": [],
        "errors": []
    }

    # Fetch item data
    items_data = []
    for item_code in item_codes:
        try:
            item = frappe.get_doc("Item", item_code)
            items_data.append({
                "item_code": item.item_code,
                "item_name": item.item_name or "",
                "item_group": item.item_group or "",
                "brand": item.brand or "",
                "description": frappe.utils.strip_html(item.description or ""),
                "standard_rate": float(item.standard_rate or 0)
            })
        except Exception as e:
            result["failed"] += 1
            result["errors"].append(f"{item_code}: {str(e)}")
            result["items"].append({"item_code": item_code, "success": False, "error": str(e)})

    if not items_data:
        return result

    # Build prompts
    system_prompt = build_batch_system_prompt(settings)
    user_prompt = build_batch_user_prompt(items_data)

    # Calculate required max_tokens (estimate ~1500 tokens per product output)
    estimated_tokens = len(items_data) * 1500
    max_tokens = min(max(estimated_tokens, 8192), 65536)  # Between 8k and 65k

    # Create batch log entry
    log = frappe.new_doc("AI Description Log")
    log.item = f"BATCH-{batch_num}-of-{total_batches}"
    log.status = "Pending"
    log.model_used = settings.gemini_model
    log.prompt_used = f"BATCH ({len(items_data)} items)\n\nSYSTEM:\n{system_prompt[:500]}...\n\nUSER:\n{user_prompt[:1000]}..."
    log.insert(ignore_permissions=True)

    try:
        # Get model with appropriate token limit
        model = get_gemini_client(max_tokens_override=max_tokens)

        full_prompt = f"{system_prompt}\n\n---\n\n{user_prompt}"

        response = model.generate_content(full_prompt)

        if not response or not response.text:
            raise Exception("Empty response from Gemini API")

        # Parse response
        parsed = parse_ai_response(response.text)

        # Update log
        log.status = "Success"
        log.response_raw = response.text[:10000]  # Truncate for storage
        if hasattr(response, 'usage_metadata'):
            log.tokens_used = response.usage_metadata.total_token_count
        log.save(ignore_permissions=True)

        # Process each product in response
        products = parsed.get("products", [])

        # Create lookup by item_code
        products_by_code = {p.get("item_code"): p for p in products}

        for item_code in item_codes:
            product_result = products_by_code.get(item_code)

            if product_result:
                try:
                    update_item_with_description(item_code, product_result, settings)
                    result["success"] += 1
                    result["items"].append({"item_code": item_code, "success": True})
                except Exception as e:
                    result["failed"] += 1
                    result["errors"].append(f"{item_code}: Update failed - {str(e)}")
                    result["items"].append({"item_code": item_code, "success": False, "error": str(e)})
            else:
                # Item not in response - try to find by partial match
                found = False
                for p in products:
                    if p.get("item_code") and item_code in p.get("item_code"):
                        try:
                            update_item_with_description(item_code, p, settings)
                            result["success"] += 1
                            result["items"].append({"item_code": item_code, "success": True})
                            found = True
                            break
                        except Exception as e:
                            pass

                if not found:
                    result["failed"] += 1
                    result["errors"].append(f"{item_code}: Not found in AI response")
                    result["items"].append({"item_code": item_code, "success": False, "error": "Not in response"})

        return result

    except Exception as e:
        log.status = "Failed"
        log.error_message = str(e)
        log.save(ignore_permissions=True)

        # Mark all items as failed
        for item_code in item_codes:
            if not any(i["item_code"] == item_code for i in result["items"]):
                result["failed"] += 1
                result["items"].append({"item_code": item_code, "success": False, "error": str(e)})

        result["errors"].append(str(e))
        raise


def run_full_batch_generation(
    limit: int = 1000,
    products_per_request: int = 20,
    item_group: str = None
) -> dict:
    """
    Run full batch generation for all pending items.

    Args:
        limit: Maximum number of items to process
        products_per_request: Items per API call (default 20)
        item_group: Optional filter by item group

    Returns:
        dict with processing results
    """
    settings = get_settings()

    # Build filters
    filters = {
        "ai_description_generated": 0,
        "disabled": 0,
        "has_variants": 0
    }

    if item_group:
        filters["item_group"] = ["like", f"%{item_group}%"]

    # Get pending items
    items = frappe.get_all(
        "Item",
        filters=filters,
        fields=["item_code"],
        limit=int(limit),
        order_by="modified desc"
    )

    item_codes = [i.item_code for i in items]

    if not item_codes:
        return {
            "success": True,
            "message": "No pending items to process",
            "total": 0
        }

    frappe.logger().info(f"AI Batch: Starting generation for {len(item_codes)} items")

    # Run multi-batch generation
    result = generate_descriptions_multi_batch(item_codes, products_per_request)

    return result
