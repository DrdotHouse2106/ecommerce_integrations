# Copyright (c) 2024, Frappe and contributors
# For license information, please see license.txt

"""
Google Gemini API Integration for Product Description Generation

Supports:
- gemini-2.5-flash (stable, default)
- gemini-2.5-flash-lite (cost-effective)
- gemini-3-flash-preview (latest, fastest - Dec 2025)
"""

import frappe
import json
import re
from typing import Optional


def get_settings():
    """Get AI Description Settings singleton"""
    return frappe.get_single("AI Description Setting")


def get_gemini_client():
    """
    Get configured Gemini GenerativeModel client

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

    # Configure generation settings
    generation_config = {
        "temperature": float(settings.temperature or 0.7),
        "max_output_tokens": int(settings.max_tokens or 4096),
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
    import time

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


def build_system_prompt(settings) -> str:
    """Build system prompt with company name substitution"""
    prompt = settings.system_prompt or ""

    # Get company name from default company or first company
    company_name = frappe.defaults.get_user_default("company")
    if not company_name:
        companies = frappe.get_all("Company", limit=1, pluck="name")
        company_name = companies[0] if companies else "Unser Unternehmen"

    prompt = prompt.replace("{company_name}", company_name)

    return prompt


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
        item: Item document
        result: Parsed AI response
        settings: AI Description Settings
    """
    if "error" in result:
        frappe.logger().error(f"AI Description: Result contains error, not updating item {item.name}")
        return

    frappe.logger().info(f"AI Description: Updating item {item.name} with AI results")

    # Update fields
    if result.get("short_description"):
        item.db_set("ai_short_description", result["short_description"])
        item.ai_short_description = result["short_description"]

    if result.get("benefits"):
        benefits_json = json.dumps(result["benefits"], ensure_ascii=False)
        item.db_set("ai_benefits", benefits_json)
        item.ai_benefits = benefits_json

    if result.get("long_description"):
        item.db_set("ai_long_description", result["long_description"])
        item.ai_long_description = result["long_description"]

    if result.get("applications"):
        item.db_set("ai_applications", result["applications"])
        item.ai_applications = result["applications"]

    if result.get("scope_of_delivery"):
        delivery_json = json.dumps(result["scope_of_delivery"], ensure_ascii=False)
        item.db_set("ai_delivery_scope", delivery_json)
        item.ai_delivery_scope = delivery_json

    if settings.include_seo:
        if result.get("seo_title"):
            item.db_set("ai_seo_title", result["seo_title"][:70])
            item.ai_seo_title = result["seo_title"][:70]
        if result.get("seo_description"):
            item.db_set("ai_seo_description", result["seo_description"][:160])
            item.ai_seo_description = result["seo_description"][:160]

    # Set metadata
    now = frappe.utils.now()
    item.db_set("ai_description_generated", 1)
    item.db_set("ai_generation_date", now)
    item.db_set("ai_model_used", settings.gemini_model)
    
    item.ai_description_generated = 1
    item.ai_generation_date = now
    item.ai_model_used = settings.gemini_model

    # Explicitly commit to database
    frappe.db.commit()
    frappe.logger().info(f"AI Description: Item {item.name} updated and committed")


def generate_descriptions_batch(item_codes: list) -> dict:
    """
    Generate descriptions for multiple items

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
