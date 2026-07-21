"""
Resync FAQ custom fields from ERPNext Item Groups to Shopware categories.

Only updates customFields (FAQ + priority) on existing categories - does NOT
create new categories or touch any other fields.

Usage:
    bench --site <your-site> execute \
        ecommerce_integrations.shopware6.scripts.resync_faq_fields.resync_faq_fields
"""

import frappe
from ecommerce_integrations.shopware6.connection import get_shopware_client
from ecommerce_integrations.shopware6.export.category_handler import (
    build_category_custom_fields,
    ensure_category_custom_field_set,
    get_item_group_data,
)
from ecommerce_integrations.shopware6.utils import create_shopware_log


def resync_faq_fields():
    """Resync FAQ fields for all categories that exist in Shopware."""
    client = get_shopware_client()

    # Ensure custom field set exists
    ensure_category_custom_field_set(client)

    # Get ALL Item Groups (not just ones with ecommerce_item - categories are separate)
    item_groups = frappe.get_all(
        "Item Group",
        filters={"is_group": 0},  # only leaf groups that can have FAQ data
        fields=["name"],
        order_by="name asc",
    )

    # Also include group-level Item Groups that might have FAQ data
    group_item_groups = frappe.get_all(
        "Item Group",
        filters={"is_group": 1},
        fields=["name"],
        order_by="name asc",
    )

    all_item_groups = item_groups + group_item_groups
    print(f"Found {len(all_item_groups)} Item Groups to check")

    updated = 0
    skipped_no_faq = 0
    skipped_no_category = 0
    errors = 0

    for ig in all_item_groups:
        ig_name = ig["name"]

        # Get item group data with FAQ fields
        ig_data = get_item_group_data(ig_name)
        if not ig_data:
            skipped_no_category += 1
            continue

        # Build custom fields (FAQ + priority)
        custom_fields = build_category_custom_fields(ig_data)
        if not custom_fields:
            skipped_no_faq += 1
            continue

        # Search for this category in Shopware
        try:
            response = client.request_post(
                "search/category",
                {"filter": [{"type": "equals", "field": "name", "value": ig_name}]}
            )
            categories = response.data or []

            if not categories:
                skipped_no_category += 1
                continue

            cat_id = categories[0]["id"]

            # PATCH only customFields
            client.request_patch(
                f"category/{cat_id}",
                {"customFields": custom_fields}
            )

            faq_count = sum(1 for k in custom_fields if "faq" in k and "question" in k and custom_fields[k])
            print(f"  Updated {ig_name} ({cat_id}) - {faq_count} FAQ(s), priority={custom_fields.get('erpnext_priority', '-')}")
            updated += 1

        except Exception as e:
            print(f"  ERROR {ig_name}: {e}")
            errors += 1

    print(f"\n--- FAQ Resync Complete ---")
    print(f"Updated: {updated}")
    print(f"Skipped (no FAQ data): {skipped_no_faq}")
    print(f"Skipped (no Shopware category): {skipped_no_category}")
    print(f"Errors: {errors}")

    create_shopware_log(
        status="Success",
        method="resync_faq_fields",
        message=f"FAQ resync: {updated} updated, {skipped_no_faq} no FAQ, {skipped_no_category} no category, {errors} errors",
        make_new=True,
    )

    frappe.db.commit()
