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
def get_gemini_client(max_tokens_override: int = None):
    """
    Get configured Gemini client and generation config

    Args:
        max_tokens_override: Override max_tokens setting (for batch processing)

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
    generation_config = types.GenerateContentConfig(
        temperature=float(settings.temperature or 0.7),
        max_output_tokens=max_tokens,
        response_mime_type="application/json"
    )

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
        client, model_name, generation_config = get_gemini_client()

        # Combine prompts for Gemini (it doesn't have separate system prompt in basic API)
        full_prompt = f"{system_prompt}\n\n---\n\n{user_prompt}"

        response = client.models.generate_content(
            model=model_name,
            contents=full_prompt,
            config=generation_config
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
    """Build system prompt with company name substitution"""
    if is_batch:
        return build_batch_system_prompt(settings)

    prompt = settings.system_prompt or ""

    # Get company name from default company or first company
    company_name = frappe.defaults.get_user_default("company")
    if not company_name:
        companies = frappe.get_all("Company", limit=1, pluck="name")
        company_name = companies[0] if companies else "Our Company"

    prompt = prompt.replace("{company_name}", company_name)

    return prompt


DEFAULT_BATCH_SYSTEM_PROMPT = '''You create structured product descriptions for an online shop.
Write in a professional, clear, and neutral tone.

## Your Task
Create precise and well-structured product descriptions from raw technical data.
You will receive MULTIPLE PRODUCTS at once and must create a separate description for EACH product.

## Target Audience
- Professional buyers in industry, trade, and logistics
- Technically knowledgeable but time-constrained
- Looking for specific solutions

## Rules
1. Highlight key features and practical benefits clearly
2. Only mention use cases that can be derived from the provided data
3. Do not invent technical specifications - only use what is in the raw data
4. No unsubstantiated quality or origin claims
5. Write without promotional exaggeration

## SEO Rules (STRICT!)
6. seo_title: EXACTLY 40-50 characters, format: "Short Name | Keyword"
7. seo_description: EXACTLY 130-155 characters, purely informative sentence
   - FORBIDDEN: Call-to-action like "Buy now", "Order today", etc.
   - Write ONLY about product properties and benefits

## HTML Formatting in long_description
Use rich HTML formatting for readability:
- <strong>...</strong> for important terms (product name, dimensions, capacity, manufacturer)
- <p>...</p> for paragraphs (at least 3-4 paragraphs)
- <ul><li>...</li></ul> for lists (e.g., scope of delivery, features)
- Highlight 3-5 key terms per description with <strong>

## Length of long_description
- Target: 200-350 words (thorough but substantive)
- Avoid marketing buzzwords and empty phrases
- Focus on: use cases, benefits, technical details, compatibility

## Output Format (JSON Array)
Respond with a JSON array. For EACH product, one object:

{
  "products": [
    {
      "item_code": "ITEM-123",
      "short_description": "1-2 sentence hook with main benefit",
      "benefits": ["Benefit 1", "Benefit 2", "Benefit 3", "Benefit 4", "Benefit 5"],
      "long_description": "<p>Detailed HTML description...</p>",
      "applications": "Application area 1, Application area 2",
      "scope_of_delivery": ["Component 1", "Component 2"],
      "seo_title": "Product Name | Keyword",
      "seo_description": "Meta description under 155 characters."
    }
  ]
}'''


def build_batch_system_prompt(settings) -> str:
    """Build system prompt for multi-product batch processing.

    Uses the batch_system_prompt from settings if configured,
    otherwise falls back to the default English prompt.
    """
    custom_prompt = getattr(settings, 'batch_system_prompt', None)
    if custom_prompt and custom_prompt.strip():
        return custom_prompt.strip()
    return DEFAULT_BATCH_SYSTEM_PROMPT


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
            error_msg = f"Batch {batch_num} failed: {e!s}"
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
        # Get client with appropriate token limit
        client, model_name, generation_config = get_gemini_client(max_tokens_override=max_tokens)
        start_time = time.time()

        full_prompt = f"{system_prompt}\n\n---\n\n{user_prompt}"

        response = client.models.generate_content(
            model=model_name,
            contents=full_prompt,
            config=generation_config
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
