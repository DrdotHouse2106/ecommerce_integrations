"""
Shopware 6 Webhook Module

Handles webhook events from Shopware with event queuing
to prevent race conditions and duplicate processing.
"""

from ecommerce_integrations.shopware6.webhook.handler import (
    WebhookHandler,
    handle_webhook_event,
)
from ecommerce_integrations.shopware6.webhook.event_queue import (
    WebhookEventQueue,
    enqueue_webhook_event,
    is_event_processed,
)

__all__ = [
    "WebhookHandler",
    "handle_webhook_event",
    "WebhookEventQueue",
    "enqueue_webhook_event",
    "is_event_processed",
]
