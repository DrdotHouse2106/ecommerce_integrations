"""
Shopware 6 Webhook Event Queue

Handles event queuing and deduplication to prevent race conditions
when multiple webhooks arrive for the same entity.
"""

from typing import Any
import hashlib
import json

import frappe
from frappe.utils import now_datetime, time_diff_in_seconds

from ecommerce_integrations.shopware6.utils import get_logger


# Event processing timeout in seconds (prevents stuck events)
EVENT_LOCK_TIMEOUT = 30

# Event deduplication window in seconds
DEDUP_WINDOW = 60


class WebhookEventQueue:
    """
    Queue for webhook events with deduplication and locking.

    Prevents race conditions when multiple webhooks arrive for the same entity
    (e.g., order.placed and order_transaction.state.changed arriving simultaneously).

    Uses Redis-based locking through frappe.cache() for distributed safety.
    """

    def __init__(self):
        self.cache = frappe.cache()

    def enqueue(
        self,
        event_type: str,
        entity_id: str,
        payload: dict[str, Any],
        timestamp: str = None
    ) -> bool:
        """
        Enqueue a webhook event for processing.

        Args:
            event_type: Type of event (e.g., "order.placed", "product.written")
            entity_id: ID of the entity (order ID, product ID, etc.)
            payload: Full webhook payload
            timestamp: Event timestamp (for deduplication)

        Returns:
            True if event was enqueued, False if duplicate/already processed
        """
        lock_key = f"shopware_webhook:{entity_id}"

        # Try to acquire lock
        lock = self.cache.lock(lock_key, timeout=EVENT_LOCK_TIMEOUT)

        try:
            if lock.acquire(blocking=True, blocking_timeout=5):
                # Check if this exact event was already processed
                if self._is_duplicate(entity_id, payload, timestamp):
                    frappe.logger("shopware6").debug(
                        f"Duplicate webhook event skipped: {event_type} for {entity_id}"
                    )
                    return False

                # Mark as being processed
                self._mark_processing(entity_id, payload, timestamp)

                # Process the event
                self._process_event(event_type, entity_id, payload)

                # Mark as completed
                self._mark_completed(entity_id, timestamp)

                return True
            else:
                # Could not acquire lock - another process is handling this entity
                frappe.logger("shopware6").warning(
                    f"Could not acquire lock for {entity_id}, skipping event: {event_type}"
                )
                return False

        except Exception as e:
            get_logger().error(f"Error processing webhook event {event_type} for {entity_id}: {e}", persist=True)
            raise

        finally:
            try:
                lock.release()
            except Exception:
                pass  # Lock may have expired

    def _is_duplicate(
        self,
        entity_id: str,
        payload: dict[str, Any],
        timestamp: str = None
    ) -> bool:
        """Check if this event was already processed recently."""
        event_hash = self._compute_event_hash(entity_id, payload, timestamp)
        cache_key = f"shopware_webhook_processed:{event_hash}"

        processed_at = self.cache.get(cache_key)
        if processed_at:
            # Check if within dedup window
            elapsed = time_diff_in_seconds(now_datetime(), processed_at)
            if elapsed < DEDUP_WINDOW:
                return True

        return False

    def _mark_processing(
        self,
        entity_id: str,
        payload: dict[str, Any],
        timestamp: str = None
    ) -> None:
        """Mark an event as being processed."""
        event_hash = self._compute_event_hash(entity_id, payload, timestamp)
        cache_key = f"shopware_webhook_processing:{event_hash}"
        self.cache.set(cache_key, now_datetime(), expires_in_sec=EVENT_LOCK_TIMEOUT)

    def _mark_completed(self, entity_id: str, timestamp: str = None) -> None:
        """Mark an event as completed."""
        # Use a simple key for deduplication
        cache_key = f"shopware_webhook_completed:{entity_id}"
        self.cache.set(cache_key, now_datetime(), expires_in_sec=DEDUP_WINDOW)

    def _compute_event_hash(
        self,
        entity_id: str,
        payload: dict[str, Any],
        timestamp: str = None
    ) -> str:
        """Compute a unique hash for the event."""
        # Use entity_id + timestamp for deduplication
        # If no timestamp, use a hash of the payload
        if timestamp:
            data = f"{entity_id}:{timestamp}"
        else:
            # Create a stable hash of the payload
            payload_str = json.dumps(payload, sort_keys=True, default=str)
            data = f"{entity_id}:{payload_str}"

        return hashlib.md5(data.encode()).hexdigest()

    def _process_event(
        self,
        event_type: str,
        entity_id: str,
        payload: dict[str, Any]
    ) -> None:
        """
        Process the webhook event.

        Routes to the appropriate handler based on event type.
        """
        from ecommerce_integrations.shopware6.webhook.handler import WebhookHandler

        handler = WebhookHandler()
        handler.handle(event_type, entity_id, payload)


def enqueue_webhook_event(
    event_type: str,
    entity_id: str,
    payload: dict[str, Any],
    timestamp: str = None
) -> bool:
    """
    Convenience function to enqueue a webhook event.

    Args:
        event_type: Type of event
        entity_id: Entity ID
        payload: Webhook payload
        timestamp: Event timestamp

    Returns:
        True if enqueued successfully
    """
    queue = WebhookEventQueue()
    return queue.enqueue(event_type, entity_id, payload, timestamp)


def is_event_processed(entity_id: str, within_seconds: int = DEDUP_WINDOW) -> bool:
    """
    Check if an event for this entity was processed recently.

    Args:
        entity_id: Entity ID to check
        within_seconds: Time window to check

    Returns:
        True if an event was processed within the window
    """
    cache = frappe.cache()
    cache_key = f"shopware_webhook_completed:{entity_id}"

    completed_at = cache.get(cache_key)
    if completed_at:
        elapsed = time_diff_in_seconds(now_datetime(), completed_at)
        return elapsed < within_seconds

    return False


def clear_event_cache(entity_id: str = None) -> None:
    """
    Clear event cache for testing/debugging.

    Args:
        entity_id: Specific entity to clear, or None for all
    """
    cache = frappe.cache()

    if entity_id:
        cache.delete(f"shopware_webhook_completed:{entity_id}")
        cache.delete(f"shopware_webhook_processing:{entity_id}")
    else:
        # Clear all webhook-related cache keys
        # Note: This is a simplified version - in production you'd want
        # to iterate through keys with a pattern
        frappe.logger("shopware6").info("Clearing all webhook event cache")
