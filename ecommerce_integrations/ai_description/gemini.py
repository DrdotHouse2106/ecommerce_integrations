# Copyright (c) 2024, Frappe and contributors
# For license information, please see license.txt

"""
Google Gemini API Integration for Product Description Generation

Supports:
- gemini-2.5-flash (stable default)
- gemini-2.5-flash-lite (lighter variant for larger batches)
- gemini-3-flash-preview (preview variant)

Features:
- Single item generation
- Multi-item batch generation (multiple products per API call for efficiency)
"""

import json
import re
import time
from typing import Any

import frappe
from frappe import _

from ecommerce_integrations.ai_description.services.logging import (
    create_ai_description_log,
    mark_ai_description_log_failed,
    mark_ai_description_log_success,
    truncate_for_log,
)


def get_settings():
    """Get AI Description Settings singleton"""
    return frappe.get_single("AI Description Setting")
def get_gemini_client(
    max_tokens_override: int | None = None,
    system_instruction: str | None = None,
):
    """Get configured Gemini client and generation config.

    Args:
        max_tokens_override: Override max_tokens setting (for batch processing).
        system_instruction: Text to pass as Gemini's ``system_instruction`` —
            the dedicated channel that survives the model's training-default
            preferences. Concatenating system+user text was found to make
            gemini-2.5-flash-lite ignore language constraints; passing the
            same text via ``system_instruction`` makes it stick.

    Returns:
        tuple: (genai.Client, model_name, generation_config)
    """
    try:
        from google import genai
        from google.genai import types
    except ImportError:
        frappe.throw(
            _("google-genai package not installed. Install with: pip install google-genai")
        )

    settings = get_settings()

    if not settings.enabled:
        frappe.throw(_("AI Description Generation is not enabled"))

    if not settings.gemini_api_key:
        frappe.throw(_("Gemini API Key not configured"))

    api_key = settings.get_password("gemini_api_key")
    client = genai.Client(api_key=api_key)

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
    config_kwargs: dict[str, Any] = {
        "temperature": float(settings.temperature or 0.7),
        "max_output_tokens": max_tokens,
        "response_mime_type": "application/json",
    }
    if system_instruction:
        config_kwargs["system_instruction"] = system_instruction
    generation_config = types.GenerateContentConfig(**config_kwargs)

    return client, model_name, generation_config


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
    log = create_ai_description_log(
        item=item_code,
        model_used=settings.gemini_model,
        prompt_used=f"SYSTEM:\n{system_prompt}\n\nUSER:\n{user_prompt}",
    )

    try:
        # System prompt goes through the dedicated ``system_instruction``
        # channel so it survives the model's training-default behaviour
        # (concatenating into the user prompt made gemini-2.5-flash-lite
        # ignore language directives).
        client, model_name, generation_config = get_gemini_client(
            system_instruction=system_prompt,
        )

        response = client.models.generate_content(
            model=model_name,
            contents=user_prompt,
            config=generation_config,
        )

        if not response or not response.text:
            raise Exception("Empty response from Gemini API")

        # Parse JSON response
        result = parse_ai_response(response.text)

        generation_time = time.time() - start_time

        mark_ai_description_log_success(
            log,
            response_text=response.text,
            generation_time=generation_time,
            tokens_used=(
                getattr(response.usage_metadata, "total_token_count", None)
                if hasattr(response, "usage_metadata") and response.usage_metadata
                else None
            ),
        )

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

        mark_ai_description_log_failed(log, error_msg, generation_time)

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
    """Build system prompt with company name substitution.

    Prepends the resolved language directive (driven by
    ``settings.output_language``) so flash-lite cannot revert to
    English when the operator's stored prompt is soft on language.
    """
    if is_batch:
        return build_batch_system_prompt(settings)

    prompt = settings.system_prompt or ""

    # Get company name from default company or first company
    company_name = frappe.defaults.get_user_default("company")
    if not company_name:
        companies = frappe.get_all("Company", limit=1, pluck="name")
        company_name = companies[0] if companies else "Our Company"

    prompt = prompt.replace("{company_name}", company_name)

    return _resolve_language_directive(settings) + prompt


DEFAULT_BATCH_SYSTEM_PROMPT = '''Du erstellst strukturierte Produktbeschreibungen für einen B2B-Online-Shop.
Schreibe in einem professionellen, klaren und sachlichen Ton.

## Deine Aufgabe
Erstelle präzise und gut strukturierte Produktbeschreibungen aus technischen Rohdaten.
Du bekommst MEHRERE PRODUKTE auf einmal und musst für JEDES eine eigene Beschreibung erzeugen.

## Zielgruppe
- Profi-Einkäufer in Industrie, Handwerk und Logistik
- Technisch versiert, aber zeitknapp
- Suchen konkrete Lösungen

## Regeln
1. Kernfeatures und konkreten Nutzen klar benennen
2. Nur Anwendungsfälle nennen, die aus den Eingabedaten hervorgehen
3. KEINE technischen Spezifikationen erfinden – nur was in den Rohdaten steht
4. Keine unbelegten Qualitäts- oder Herkunftsbehauptungen
5. Ohne Werbe-Übertreibungen schreiben

## SEO-Regeln (STRIKT!)
6. seo_title: GENAU 40-50 Zeichen, Format: "Kurzer Name | Keyword"
7. seo_description: GENAU 130-155 Zeichen, rein informativer Satz
   - VERBOTEN: Call-to-Action wie "Jetzt kaufen", "Heute bestellen", etc.
   - NUR über Produkteigenschaften und Nutzen schreiben

## HTML-Formatierung in long_description
Nutze HTML für Lesbarkeit:
- <strong>...</strong> für wichtige Begriffe (Produktname, Maße, Tragkraft, Hersteller)
- <p>...</p> für Absätze (mindestens 3-4 Absätze)
- <ul><li>...</li></ul> für Listen (z.B. Lieferumfang, Features)
- 3-5 Schlüsselbegriffe pro Beschreibung mit <strong> hervorheben

## Länge von long_description
- Ziel: 200-350 Wörter (ausführlich aber substanziell)
- Vermeide Marketing-Floskeln und leere Phrasen
- Fokus auf: Anwendungsfälle, Nutzen, technische Details, Kompatibilität

## Output-Format (JSON Array)
Antworte mit einem JSON-Objekt. Für JEDES Produkt ein Eintrag im products-Array:

{
  "products": [
    {
      "item_code": "ITEM-123",
      "short_description": "1-2 Sätze Hook mit Hauptnutzen",
      "benefits": ["Vorteil 1", "Vorteil 2", "Vorteil 3", "Vorteil 4", "Vorteil 5"],
      "long_description": "<p>Ausführliche HTML-Beschreibung…</p>",
      "applications": "Anwendungsbereich 1, Anwendungsbereich 2",
      "scope_of_delivery": ["Komponente 1", "Komponente 2"],
      "seo_title": "Produktname | Keyword",
      "seo_description": "Meta-Beschreibung unter 155 Zeichen."
    }
  ]
}'''


# Per-language hard directive prepended to whatever prompt the operator
# configured. Gemini's flash-lite variant has been observed to revert to
# English output when the prompt is "merely written in" the target
# language; only an explicit "WICHTIG: ... AUSSCHLIESSLICH …" / "IMPORTANT:
# ... ONLY …" sticks. The directive is loaded via Gemini's dedicated
# ``system_instruction`` channel (not concatenated into the user prompt)
# because that's where Gemini gives it highest priority.
_LANGUAGE_DIRECTIVES: dict[str, str] = {
    "de": (
        "WICHTIG: Schreibe ALLE Ausgabefelder (short_description, benefits, "
        "long_description, applications, scope_of_delivery, seo_title, "
        "seo_description) AUSSCHLIESSLICH AUF DEUTSCH. Englischer Output "
        "gilt als Fehler und wird abgelehnt. Produktnamen aus dem item_name "
        "dürfen in der Originalsprache bleiben.\n\n"
    ),
    "en": (
        "IMPORTANT: Write ALL output fields (short_description, benefits, "
        "long_description, applications, scope_of_delivery, seo_title, "
        "seo_description) IN ENGLISH ONLY. Output in any other language "
        "counts as an error and will be rejected. Product names from the "
        "item_name field may remain in their original language.\n\n"
    ),
    "fr": (
        "IMPORTANT : Rédigez TOUS les champs de sortie (short_description, "
        "benefits, long_description, applications, scope_of_delivery, "
        "seo_title, seo_description) EXCLUSIVEMENT EN FRANÇAIS. Une sortie "
        "dans une autre langue est considérée comme une erreur. Les noms "
        "de produits du champ item_name peuvent rester dans leur langue "
        "d'origine.\n\n"
    ),
    "it": (
        "IMPORTANTE: Scrivi TUTTI i campi di output (short_description, "
        "benefits, long_description, applications, scope_of_delivery, "
        "seo_title, seo_description) ESCLUSIVAMENTE IN ITALIANO. Output in "
        "altre lingue è considerato un errore. I nomi prodotto dal campo "
        "item_name possono rimanere nella lingua originale.\n\n"
    ),
    "es": (
        "IMPORTANTE: Escribe TODOS los campos de salida (short_description, "
        "benefits, long_description, applications, scope_of_delivery, "
        "seo_title, seo_description) EXCLUSIVAMENTE EN ESPAÑOL. La salida "
        "en cualquier otro idioma se considera un error. Los nombres de "
        "producto del campo item_name pueden permanecer en su idioma "
        "original.\n\n"
    ),
    "nl": (
        "BELANGRIJK: Schrijf ALLE outputvelden (short_description, "
        "benefits, long_description, applications, scope_of_delivery, "
        "seo_title, seo_description) UITSLUITEND IN HET NEDERLANDS. Output "
        "in een andere taal geldt als fout. Productnamen uit het "
        "item_name-veld mogen in de originele taal blijven.\n\n"
    ),
}


def _resolve_language_directive(settings) -> str:
    """Resolve the language directive for the configured output language.

    Reads ``settings.target_language`` (the Select on the doctype, options
    ``German|English|French|Spanish|Italian|Dutch``). Empty / unknown
    values fall through to no directive (Gemini's training-default
    behaviour, usually English). Accepts the doctype labels plus ISO-639-1
    codes and a few German aliases (case-insensitive).
    """
    raw = (getattr(settings, "target_language", None)
           or getattr(settings, "output_language", None)
           or "").strip().lower()
    if not raw:
        return ""
    aliases = {
        "german": "de", "deutsch": "de", "de_de": "de",
        "english": "en", "englisch": "en", "en_us": "en", "en_gb": "en",
        "french": "fr", "französisch": "fr", "francais": "fr", "fr_fr": "fr",
        "italian": "it", "italienisch": "it", "italiano": "it", "it_it": "it",
        "spanish": "es", "spanisch": "es", "español": "es", "espanol": "es", "es_es": "es",
        "dutch": "nl", "niederländisch": "nl", "nederlands": "nl", "nl_nl": "nl",
    }
    code = aliases.get(raw, raw)
    return _LANGUAGE_DIRECTIVES.get(code, "")


def build_batch_system_prompt(settings) -> str:
    """Build system prompt for multi-product batch processing.

    Uses ``batch_system_prompt`` from settings when configured, else the
    bundled default. Prepends a hard language directive when
    ``settings.output_language`` is set — flash-lite otherwise drifts to
    English regardless of the rest of the prompt.
    """
    custom_prompt = getattr(settings, 'batch_system_prompt', None)
    base = (custom_prompt or "").strip() or DEFAULT_BATCH_SYSTEM_PROMPT
    return _resolve_language_directive(settings) + base


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


def build_batch_user_prompt(items: list[dict]) -> str:
    """Build user prompt for multiple products"""
    prompt_parts = ["## Product Data\n\nCreate descriptions for the following products:\n"]

    for i, item in enumerate(items, 1):
        prompt_parts.append(f"""
---
### Product {i}: {item.get('item_code', 'N/A')}

**Item Code:** {item.get('item_code', '')}
**Item Name:** {item.get('item_name', '')}
**Product Group:** {item.get('item_group', '')}
**Brand:** {item.get('brand', '')}
**Technical Data:** {item.get('description', '')}
**Price:** {item.get('standard_rate', 0)}
""")

    prompt_parts.append("\n---\n\nCreate optimized descriptions for ALL products listed above in JSON format.")

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
        frappe.logger().error("AI Description: Result contains error, not updating item")
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


def _is_rate_limit_error(exc: BaseException) -> bool:
    """Detect Gemini 429 / RESOURCE_EXHAUSTED in an exception."""
    msg = str(exc) or ""
    if "RESOURCE_EXHAUSTED" in msg or "exceeded your current quota" in msg:
        return True
    if "429" in msg and ("rate" in msg.lower() or "quota" in msg.lower()):
        return True
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    return code == 429


def _extract_retry_delay_seconds(exc: BaseException) -> float | None:
    """Parse the ``retryDelay`` hint Gemini puts on 429 responses.

    Returns ``None`` when no hint is present.
    """
    msg = str(exc) or ""
    m = re.search(r"retryDelay['\"]?\s*[:=]\s*['\"]?(\d+)s", msg)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    return None


_RATE_LIMIT_BACKOFF_SCHEDULE = (60.0, 120.0, 240.0, 480.0, 900.0)
_RATE_LIMIT_BACKOFF_MAX = 1800.0  # cap individual sleeps at 30 min
_RATE_LIMIT_GIVE_UP_AFTER = len(_RATE_LIMIT_BACKOFF_SCHEDULE)


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
        "errors": [],
        "aborted_reason": None,
    }

    # Split into batches
    batches = [item_codes[i:i + products_per_request] for i in range(0, len(item_codes), products_per_request)]
    total_batches = len(batches)

    frappe.logger().info(f"AI Multi-Batch: Processing {len(item_codes)} items in {total_batches} batches of up to {products_per_request}")

    batch_iter = enumerate(batches, 1)
    while True:
        try:
            batch_num, batch = next(batch_iter)
        except StopIteration:
            break

        batch_start = time.time()

        try:
            batch_result, rate_limited_attempts = _process_multi_batch_with_backoff(
                batch, settings, batch_num, total_batches
            )
        except _GeminiRateLimitGaveUp as gaveup:
            # Repeated 429s — stop the run entirely rather than burn through
            # the remaining batches generating empty Failed logs. The remaining
            # items are reported as skipped so the caller can resume tomorrow.
            error_msg = f"Aborted after repeated rate-limit hits: {gaveup}"
            frappe.logger().warning(f"AI Multi-Batch: {error_msg}")
            results["errors"].append(error_msg)
            results["aborted_reason"] = "rate_limit"
            # Current batch + all remaining ones go down as skipped
            remaining = [batch] + [b for _, b in batch_iter]
            for skipped_batch in remaining:
                results["failed"] += len(skipped_batch)
                for item_code in skipped_batch:
                    results["items"].append({
                        "item_code": item_code,
                        "success": False,
                        "error": "skipped_rate_limit",
                    })
            break
        except Exception as e:
            error_msg = f"Batch {batch_num} failed: {e!s}"
            frappe.logger().error(f"AI Multi-Batch: {error_msg}")
            results["errors"].append(error_msg)
            results["failed"] += len(batch)
            for item_code in batch:
                results["items"].append({
                    "item_code": item_code,
                    "success": False,
                    "error": error_msg,
                })
            continue

        results["success"] += batch_result["success"]
        results["failed"] += batch_result["failed"]
        results["batches_processed"] += 1
        results["items"].extend(batch_result.get("items", []))

        if batch_result.get("errors"):
            results["errors"].extend(batch_result["errors"])

        batch_time = time.time() - batch_start
        suffix = f" (after {rate_limited_attempts} backoff retr{'y' if rate_limited_attempts == 1 else 'ies'})" if rate_limited_attempts else ""
        frappe.logger().info(
            f"AI Multi-Batch: Batch {batch_num}/{total_batches} completed in {batch_time:.1f}s"
            f" - {batch_result['success']} success, {batch_result['failed']} failed{suffix}"
        )

        # Commit after each batch
        frappe.db.commit()

        # Progress update
        frappe.publish_realtime(
            "ai_description_progress",
            {
                "batch": batch_num,
                "total_batches": total_batches,
                "items_processed": results["success"] + results["failed"],
                "total_items": len(item_codes),
            },
            user=frappe.session.user,
        )

    total_time = time.time() - start_time
    results["total_time"] = total_time
    results["avg_time_per_item"] = total_time / len(item_codes) if item_codes else 0

    frappe.logger().info(f"AI Multi-Batch: Completed {len(item_codes)} items in {total_time:.1f}s ({results['avg_time_per_item']:.2f}s/item)")

    return results


class _GeminiRateLimitGaveUp(Exception):
    """Raised when a batch has hit 429 more times than the backoff schedule allows."""


def _process_multi_batch_with_backoff(
    batch: list, settings, batch_num: int, total_batches: int
) -> tuple[dict, int]:
    """Wrap ``_process_multi_batch`` with exponential backoff on Gemini 429s.

    Returns ``(batch_result, rate_limited_attempts)`` where ``rate_limited_attempts``
    is the number of times we slept and retried before this batch succeeded.

    Raises ``_GeminiRateLimitGaveUp`` if we exhaust the schedule.
    """
    attempts = 0
    for delay in _RATE_LIMIT_BACKOFF_SCHEDULE + (None,):  # None = final attempt then give up
        try:
            return _process_multi_batch(batch, settings, batch_num, total_batches), attempts
        except Exception as e:
            if not _is_rate_limit_error(e):
                raise
            if delay is None:
                raise _GeminiRateLimitGaveUp(
                    f"batch {batch_num}/{total_batches}: {_RATE_LIMIT_GIVE_UP_AFTER} 429s in a row"
                ) from e
            attempts += 1
            # Prefer Gemini's own retryDelay hint when present, else schedule.
            hint = _extract_retry_delay_seconds(e)
            sleep_for = max(min(hint or delay, _RATE_LIMIT_BACKOFF_MAX), 1.0)
            frappe.logger().warning(
                f"AI Multi-Batch: rate-limited on batch {batch_num}/{total_batches}; "
                f"sleeping {sleep_for:.0f}s then retrying (attempt {attempts})"
            )
            # Don't hold a DB connection across the sleep; MySQL may drop it.
            try:
                frappe.db.commit()
            except Exception:
                pass
            time.sleep(sleep_for)


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
            result["errors"].append(f"{item_code}: {e!s}")
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
    log = create_ai_description_log(
        item=f"BATCH-{batch_num}-of-{total_batches}",
        model_used=settings.gemini_model,
        prompt_used=f"BATCH ({len(items_data)} items)\n\nSYSTEM:\n{system_prompt[:500]}...\n\nUSER:\n{user_prompt[:1000]}...",
    )

    try:
        # Get client with appropriate token limit. Pass the system prompt
        # via the dedicated ``system_instruction`` channel — concatenating
        # it into the user prompt caused gemini-2.5-flash-lite to ignore
        # the German language directive and emit English output.
        client, model_name, generation_config = get_gemini_client(
            max_tokens_override=max_tokens,
            system_instruction=system_prompt,
        )
        start_time = time.time()

        response = client.models.generate_content(
            model=model_name,
            contents=user_prompt,
            config=generation_config,
        )

        if not response or not response.text:
            raise Exception("Empty response from Gemini API")

        # Parse response
        parsed = parse_ai_response(response.text)

        mark_ai_description_log_success(
            log,
            response_text=truncate_for_log(response.text, 10000),
            generation_time=time.time() - start_time,
            tokens_used=(
                getattr(response.usage_metadata, "total_token_count", None)
                if hasattr(response, "usage_metadata") and response.usage_metadata
                else None
            ),
        )

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
                    result["errors"].append(f"{item_code}: Update failed - {e!s}")
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
                        except Exception:
                            pass

                if not found:
                    result["failed"] += 1
                    result["errors"].append(f"{item_code}: Not found in AI response")
                    result["items"].append({"item_code": item_code, "success": False, "error": "Not in response"})

        return result

    except Exception as e:
        generation_time = time.time() - start_time if "start_time" in locals() else None
        mark_ai_description_log_failed(log, str(e), generation_time)

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
    item_group: str | None = None
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
