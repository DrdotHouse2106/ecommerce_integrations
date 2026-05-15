"""
Shopware 6 Webhook Module

Handles webhook events from Shopware with event queuing
to prevent race conditions and duplicate processing.
"""

from ecommerce_integrations.shopware6.webhook.event_queue import (
    WebhookEventQueue,
    enqueue_webhook_event,
    is_event_processed,
)
from ecommerce_integrations.shopware6.webhook.handler import (
    WebhookHandler,
    handle_webhook_event,
)

__all__ = [
    "WebhookEventQueue",
    "WebhookHandler",
    "enqueue_webhook_event",
    "handle_webhook_event",
    "is_event_processed",
]
