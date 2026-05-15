# Copyright (c) 2024, Frappe and contributors
# For license information, please see license.txt

"""
Scheduler functions for AI Description batch processing

Called hourly by Frappe's scheduler.
"""

from datetime import datetime, timedelta

import frappe


def process_batch_descriptions():
    """
    Process batch AI description generation

    Called hourly by scheduler. Checks if batch processing is enabled
    and if the configured interval has passed since last run.
    """
    try:
        settings = frappe.get_single("AI Description Setting")

        # Check if enabled
        if not settings.enabled:
            return

        if not settings.enable_batch_processing:
            return

        # Check interval (default 60 minutes)
        interval_minutes = settings.process_interval_minutes or 60

        if settings.last_batch_run:
            last_run = frappe.utils.get_datetime(settings.last_batch_run)
            next_run = last_run + timedelta(minutes=interval_minutes)

            if datetime.now() < next_run:
                # Not time to run yet
                return

        # Get pending items
        from .api import get_pending_items
        from .gemini import generate_descriptions_batch

        batch_size = settings.batch_size or 10
        item_codes = get_pending_items(limit=batch_size)

        if not item_codes:
            frappe.logger().info("AI Description: No items pending generation")
            return

        frappe.logger().info(f"AI Description: Processing {len(item_codes)} items")

        # Generate descriptions
        result = generate_descriptions_batch(item_codes)

        # Update settings
        settings.reload()
        settings.last_batch_run = frappe.utils.now()
        settings.items_processed = (settings.items_processed or 0) + result["success"]
        settings.save(ignore_permissions=True)
        frappe.db.commit()

        frappe.logger().info(
            f"AI Description: Batch complete. "
            f"Success: {result['success']}, Failed: {result['failed']}"
        )

    except Exception as e:
        frappe.log_error(
            title="AI Description Batch Processing Error",
            message=str(e)
        )
