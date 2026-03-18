"""Logging helpers for AI description generation."""

import frappe

MAX_PROMPT_LOG_LENGTH = 12000
MAX_RESPONSE_LOG_LENGTH = 12000


def truncate_for_log(value: str, limit: int) -> str:
    """Trim large prompt/response payloads before storing them."""
    if len(value) <= limit:
        return value
    return f"{value[:limit]}... [truncated]"


def create_ai_description_log(item: str, model_used: str, prompt_used: str):
    """Create a pending AI description log entry."""
    log = frappe.new_doc("AI Description Log")
    log.item = item
    log.status = "Pending"
    log.model_used = model_used
    log.prompt_used = truncate_for_log(prompt_used, MAX_PROMPT_LOG_LENGTH)
    log.insert(ignore_permissions=True)
    return log


def mark_ai_description_log_success(log, response_text: str, generation_time: float, tokens_used=None) -> None:
    """Mark an AI description log as successful."""
    log.status = "Success"
    log.generation_time = generation_time
    log.response_raw = truncate_for_log(response_text, MAX_RESPONSE_LOG_LENGTH)
    if tokens_used is not None:
        log.tokens_used = tokens_used
    log.save(ignore_permissions=True)


def mark_ai_description_log_failed(log, error_message: str, generation_time: float | None = None) -> None:
    """Mark an AI description log as failed."""
    log.status = "Failed"
    if generation_time is not None:
        log.generation_time = generation_time
    log.error_message = error_message
    log.save(ignore_permissions=True)
