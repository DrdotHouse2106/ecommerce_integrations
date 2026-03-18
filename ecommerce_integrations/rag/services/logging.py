"""Logging helpers for RAG sync."""

import json

import frappe

SENSITIVE_KEY_PARTS = ("password", "secret", "token", "api_key", "api-key", "authorization")
MAX_LOG_STRING_LENGTH = 4000


def create_rag_log(
    status,
    method,
    item_code=None,
    request_data=None,
    response_data=None,
    message=None,
    exception=None,
):
    """Create a sanitized RAG log entry."""
    try:
        log = frappe.new_doc("RAG Log")
        log.status = status
        log.method = method
        log.item_code = item_code
        log.request_data = _serialize_log_data(request_data)
        log.response_data = _serialize_log_data(response_data)
        log.message = _truncate_log_text(message, 65000) if message else None
        log.exception = _truncate_log_text(str(exception), 65000) if exception else None
        log.insert(ignore_permissions=True)
        frappe.db.commit()
        return log.name
    except Exception as exc:
        frappe.logger().error(f"Failed to create RAG log: {exc}")
        return None


def _serialize_log_data(data):
    if not data:
        return None
    return json.dumps(_sanitize_log_data(data), default=str)[:65000]


def _sanitize_log_data(data):
    if isinstance(data, dict):
        sanitized = {}
        for key, value in data.items():
            lowered = str(key).lower()
            if any(part in lowered for part in SENSITIVE_KEY_PARTS):
                sanitized[key] = "[redacted]"
            else:
                sanitized[key] = _sanitize_log_data(value)
        return sanitized

    if isinstance(data, (list, tuple, set)):
        return [_sanitize_log_data(value) for value in list(data)[:50]]

    if isinstance(data, str):
        return _truncate_log_text(data, MAX_LOG_STRING_LENGTH)

    return data


def _truncate_log_text(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return f"{value[:limit]}... [truncated]"
