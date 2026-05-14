"""Helpers for sanitizing Medusa log payloads.

Ported from ``shopware6/services/log_sanitizer.py`` — the Medusa module
adds ``publishable_api_key`` to the sensitive-key set since Medusa storefronts
authenticate with both an API token and a publishable key.
"""

import hashlib
import re
from typing import Any

SENSITIVE_KEY_PARTS = (
    "authorization",
    "password",
    "secret",
    "token",
    "api_key",
    "api-key",
    "access_key",
    "refresh_key",
    "refresh_token",
    # Medusa-specific
    "publishable_api_key",
    "publishable-api-key",
)
MAX_LOG_STRING_LENGTH = 4000
MAX_LOG_LIST_ITEMS = 50


def sanitize_medusa_log_data(data: Any) -> Any:
    """Redact secrets and summarize binary payloads before persisting logs."""
    return _sanitize_log_value(data)


def sanitize_medusa_log_text(value: str) -> str:
    """Redact secrets from free-form log text."""
    if not isinstance(value, str):
        return value
    value = re.sub(r"(?i)(bearer|basic)\s+[a-z0-9._~+/=-]+", r"\1 [redacted]", value)
    value = re.sub(
        r"(?i)((?:access|refresh|api|client|webhook|publishable)?[_-]?(?:token|secret|key|password)\s*[:=]\s*)([^,\s]+)",
        r"\1[redacted]",
        value,
    )
    if len(value) > MAX_LOG_STRING_LENGTH:
        return f"{value[:MAX_LOG_STRING_LENGTH]}... [truncated]"
    return value


def _sanitize_log_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return {
            "type": "bytes",
            "length": len(value),
            "sha256": hashlib.sha256(value).hexdigest(),
        }

    if isinstance(value, dict):
        sanitized: dict[Any, Any] = {}
        for key, item in value.items():
            if _is_sensitive_key(str(key)):
                sanitized[key] = "[redacted]"
            else:
                sanitized[key] = _sanitize_log_value(item)
        return sanitized

    if isinstance(value, (list, tuple, set)):
        items = list(value)
        sanitized_items = [_sanitize_log_value(item) for item in items[:MAX_LOG_LIST_ITEMS]]
        if len(items) > MAX_LOG_LIST_ITEMS:
            sanitized_items.append(f"... truncated {len(items) - MAX_LOG_LIST_ITEMS} more entries")
        return sanitized_items

    if isinstance(value, str):
        return sanitize_medusa_log_text(value)

    return value


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in SENSITIVE_KEY_PARTS)
