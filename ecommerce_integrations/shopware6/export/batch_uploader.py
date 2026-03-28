"""
Shopware 6 Batch Product Uploader

Uses Shopware's _action/sync API for bulk operations.
Significantly faster than individual POST/PATCH calls.

Key improvements over sequential upload:
1. Single HTTP request for up to 100 products
2. Batch reads from ERPNext database
3. Cached lookups for categories, manufacturers, taxes
"""

import time
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field

import frappe
from frappe.utils import flt

# CRITICAL: ShopwareAPIError inherits from BaseException, NOT Exception!
from lib_shopware6_api_base.conf_shopware6_api_base_classes import ShopwareAPIError

from ecommerce_integrations.shopware6.constants import (
    MODULE_NAME,
    SETTING_DOCTYPE,
    ITEM_SELLING_RATE_FIELD,
    PRODUCT_CUSTOM_FIELDS_MAP,
)
from ecommerce_integrations.shopware6.utils import get_logger, create_shopware_log
from ecommerce_integrations.shopware6.export.utils import generate_uuid, get_shopware_document_id
from ecommerce_integrations.shopware6.export.product_mapper import get_product_visibilities, get_actual_variant_values
from ecommerce_integrations.shopware6.base.cache_manager import get_cache


# Maximum products per sync request (Shopware limit is ~500, but 100 is safer)
BATCH_SIZE = 100

# Lock timeout retry settings
LOCK_TIMEOUT_MAX_RETRIES = 3
LOCK_TIMEOUT_RETRY_DELAY = 5  # seconds


def _sync_with_retry(client, sync_payload: dict, logger, context: str = "sync") -> dict:
    """
    Execute Shopware sync API call with retry logic for lock timeouts
    and SEO URL duplicate key errors.

    Uses 'use-queue-indexing' header to defer SEO URL generation to the
    message queue, avoiding duplicate key errors during batch operations.

    Args:
        client: Shopware API client
        sync_payload: The sync payload to send
        logger: Logger instance
        context: Description for error messages

    Returns:
        API response dict

    Raises:
        Exception: If max retries exceeded for lock timeout
    """
    # Use queue-based indexing to avoid SEO URL duplicate key errors
    # during batch sync. Shopware will generate SEO URLs asynchronously.
    sync_headers = {"indexing-behavior": "use-queue-indexing"}

    for attempt in range(1, LOCK_TIMEOUT_MAX_RETRIES + 1):
        try:
            return client.request_post("_action/sync", sync_payload, update_header_fields=sync_headers)
        except (Exception, ShopwareAPIError) as e:
            error_str = str(e)
            is_lock_timeout = "1205" in error_str or "Lock wait timeout" in error_str
            is_seo_duplicate = "1062" in error_str and "seo_url" in error_str

            if is_lock_timeout and attempt < LOCK_TIMEOUT_MAX_RETRIES:
                logger.warning(f"Lock timeout on {context} (attempt {attempt}/{LOCK_TIMEOUT_MAX_RETRIES}), retrying in {LOCK_TIMEOUT_RETRY_DELAY}s...")
                time.sleep(LOCK_TIMEOUT_RETRY_DELAY)
                continue

            if is_seo_duplicate and attempt < LOCK_TIMEOUT_MAX_RETRIES:
                logger.warning(f"SEO URL duplicate on {context} (attempt {attempt}), retrying in {LOCK_TIMEOUT_RETRY_DELAY}s...")
                time.sleep(LOCK_TIMEOUT_RETRY_DELAY)
                continue

            if is_lock_timeout or is_seo_duplicate:
                error_type = "Lock timeout" if is_lock_timeout else "SEO URL duplicate"
                error_msg = f"{error_type} after {LOCK_TIMEOUT_MAX_RETRIES} retries for {context}"
                logger.error(error_msg)
                create_shopware_log(
                    status="Error",
                    method="batch_uploader_sync",
                    message=error_msg,
                    request_data={"context": context, "error": error_str[:500]}
                )
                raise Exception(error_msg)

            # Non-retriable error: re-raise immediately
            raise


@dataclass
class BatchUploadResult:
    """Result of a batch upload operation."""
    total: int = 0
    success: int = 0
    failed: int = 0
    skipped: int = 0
    errors: List[Dict[str, Any]] = field(default_factory=list)
    created_ids: List[str] = field(default_factory=list)  # Shopware IDs
    updated_ids: List[str] = field(default_factory=list)  # Shopware IDs
    processed_item_codes: List[str] = field(default_factory=list)  # ERPNext item codes
    skipped_items: List[Dict[str, str]] = field(default_factory=list)  # {item_code, reason}


class BatchProductUploader:
    """
    High-performance batch product uploader using Shopware Sync API.

    Usage:
        uploader = BatchProductUploader()
        result = uploader.upload_items(["ITEM-001", "ITEM-002", ...])

        # With progress callback:
        def on_progress(batch_num, total_batches, processed, total, success, failed):
            print(f"Batch {batch_num}/{total_batches}: {processed}/{total}")

        uploader = BatchProductUploader(progress_callback=on_progress)
    """

    def __init__(self, progress_callback: Optional[callable] = None):
        """
        Initialize the batch uploader.

        Args:
            progress_callback: Optional callback function(batch_num, total_batches, processed, total, success, failed)
        """
        self.setting = frappe.get_cached_doc(SETTING_DOCTYPE)
        self.logger = get_logger("BatchUploader")
        self.cache = get_cache()
        self.progress_callback = progress_callback

        # Pre-cached lookups (populated once per batch)
        self._currency_id: Optional[str] = None
        self._tax_ids: Dict[float, str] = {}
        self._manufacturer_ids: Dict[str, str] = {}
        self._category_cache: Dict[str, List[str]] = {}
        self._existing_products: Dict[str, str] = {}  # item_code -> shopware_id

        # Bulk property caches (populated per template batch)
        self._property_groups: Dict[str, str] = {}  # group_name -> group_id
        self._property_options: Dict[str, str] = {}  # "group_name:option_value" -> option_id

    def upload_items(self, item_codes: List[str], skip_images: bool = False, start_batch: int = 1) -> BatchUploadResult:
        """
        Upload multiple items to Shopware in batches.

        Args:
            item_codes: List of ERPNext item codes to upload
            skip_images: Skip image sync for faster upload
            start_batch: Start from this batch number (1-based, for resuming interrupted syncs)

        Returns:
            BatchUploadResult with statistics
        """
        from ecommerce_integrations.shopware6.connection import get_shopware_client

        result = BatchUploadResult(total=len(item_codes))

        if not item_codes:
            return result

        client = get_shopware_client()
        if not client:
            self.logger.error("Could not get Shopware client")
            return result

        # Initialize caches
        self._init_caches(client)

        # Validate critical cache values - currency_id is required for price payloads
        if not self._currency_id:
            self.logger.error("Failed to get EUR currency ID from Shopware - cannot sync items without valid currency")
            result.failed = len(item_codes)
            result.errors.append({
                "item_code": "INIT",
                "error": "Currency ID lookup failed - ensure EUR currency exists in Shopware"
            })
            return result

        # Load existing Ecommerce Item mappings
        self._load_existing_mappings(item_codes)

        # Reconcile with Shopware - find products that exist in Shopware but not in ERPNext mappings
        self._reconcile_with_shopware(client, item_codes)

        # Process in batches
        total_batches = (len(item_codes) + BATCH_SIZE - 1) // BATCH_SIZE

        # Calculate starting position (start_batch is 1-based)
        start_index = (start_batch - 1) * BATCH_SIZE if start_batch > 1 else 0
        if start_batch > 1:
            self.logger.info(f"Resuming from batch {start_batch}/{total_batches} (skipping first {start_index} items)")

        for batch_start in range(start_index, len(item_codes), BATCH_SIZE):
            batch_codes = item_codes[batch_start:batch_start + BATCH_SIZE]
            batch_num = batch_start // BATCH_SIZE + 1

            try:
                batch_result = self._process_batch(client, batch_codes, skip_images)

                result.success += batch_result.success
                result.failed += batch_result.failed
                result.skipped += batch_result.skipped
                result.errors.extend(batch_result.errors)
                result.created_ids.extend(batch_result.created_ids)
                result.updated_ids.extend(batch_result.updated_ids)
                result.processed_item_codes.extend(batch_result.processed_item_codes)
                result.skipped_items.extend(batch_result.skipped_items)

                # Commit after each batch
                frappe.db.commit()

                processed = min(batch_start + BATCH_SIZE, len(item_codes))
                synced_codes = ", ".join(batch_result.processed_item_codes[:5])
                if len(batch_result.processed_item_codes) > 5:
                    synced_codes += f"... (+{len(batch_result.processed_item_codes) - 5} more)"

                # Log to file
                self.logger.info(
                    f"Batch {batch_num}/{total_batches}: "
                    f"{batch_result.success} success, {batch_result.failed} failed, {batch_result.skipped} skipped. "
                    f"Items: {synced_codes}"
                )

                # Log to Ecommerce Integration Log (like category sync does)
                batch_summary = (
                    f"Simple Items Batch {batch_num}/{total_batches}: "
                    f"{batch_result.success} success, {batch_result.failed} failed, {batch_result.skipped} skipped"
                )
                create_shopware_log(
                    status="Success" if batch_result.failed == 0 else "Partial Success",
                    method="BatchProductUploader.upload_items.batch",
                    message=batch_summary,
                    request_data={
                        "batch_num": batch_num,
                        "total_batches": total_batches,
                        "batch_size": len(batch_codes),
                        "success": batch_result.success,
                        "failed": batch_result.failed,
                        "skipped": batch_result.skipped,
                        "items": batch_result.processed_item_codes[:20],  # Limit to first 20
                        "errors": batch_result.errors[:5] if batch_result.errors else [],
                        "skipped_items": batch_result.skipped_items[:10] if batch_result.skipped_items else []
                    }
                )

                # Call progress callback if provided (full_batch_sync has its own phase logging)
                if self.progress_callback:
                    try:
                        self.progress_callback(
                            batch_num,
                            total_batches,
                            processed,
                            len(item_codes),
                            result.success,
                            result.failed
                        )
                    except Exception as e:
                        self.logger.warning(f"Progress callback failed: {e}")

            except Exception as batch_error:
                # Log error but CONTINUE with next batch instead of aborting
                error_msg = str(batch_error)[:500]
                self.logger.error(
                    f"Batch {batch_num}/{total_batches} FAILED with error: {error_msg}. "
                    f"Continuing with next batch..."
                )
                result.failed += len(batch_codes)
                for item_code in batch_codes:
                    result.errors.append({"item_code": item_code, "error": f"Batch failed: {error_msg[:100]}"})

                # Log batch failure
                create_shopware_log(
                    status="Error",
                    method="BatchProductUploader.upload_items.batch",
                    message=f"Simple Items Batch {batch_num}/{total_batches} FAILED: {error_msg[:200]}",
                    request_data={
                        "batch_num": batch_num,
                        "total_batches": total_batches,
                        "batch_size": len(batch_codes),
                        "items": batch_codes[:20],
                        "error": error_msg
                    }
                )

                # Try to commit any partial work and reconnect if needed
                try:
                    frappe.db.commit()
                except Exception:
                    try:
                        frappe.db.rollback()
                        frappe.connect()
                    except Exception:
                        pass

        return result

    def _init_caches(self, client) -> None:
        """Initialize cached lookups that are reused across products."""
        from ecommerce_integrations.shopware6.export.property_handler import ensure_shopware_custom_field_set

        # Currency
        self._currency_id = self._get_currency_id(client, "EUR")

        # Pre-load common tax rates
        for rate in [19.0, 7.0, 0.0]:
            self._tax_ids[rate] = self._get_tax_id(client, rate)

        # Ensure custom field set exists in Shopware (for AI Benefits, etc.)
        try:
            ensure_shopware_custom_field_set(client)
        except Exception as e:
            self.logger.warning(f"Could not ensure custom field set: {e}")

    def _load_existing_mappings(self, item_codes: List[str]) -> None:
        """Load existing Ecommerce Item mappings in batch."""
        if not item_codes:
            return

        mappings = frappe.get_all(
            "Ecommerce Item",
            filters={
                "integration": MODULE_NAME,
                "erpnext_item_code": ["in", item_codes]
            },
            fields=["erpnext_item_code", "integration_item_code"]
        )

        self._existing_products = {
            m.erpnext_item_code: m.integration_item_code
            for m in mappings
        }

    def _reconcile_with_shopware(self, client, item_codes: List[str]) -> None:
        """
        Lookup products in Shopware by productNumber and reconcile mappings.
        This ensures we always update existing products even if Ecommerce Item mapping is missing.
        """
        if not item_codes:
            return

        # Find item_codes that are NOT in existing mappings
        missing_codes = [code for code in item_codes if code not in self._existing_products]

        if not missing_codes:
            return

        # Batch lookup in Shopware by productNumber
        try:
            # Shopware search with multiple productNumbers
            search_payload = {
                "filter": [
                    {
                        "type": "equalsAny",
                        "field": "productNumber",
                        "value": missing_codes
                    }
                ],
                "includes": {
                    "product": ["id", "productNumber", "parentId"]
                },
                "limit": len(missing_codes)
            }

            response = client.request_post("search/product", search_payload)
            shopware_products = response.get("data", [])

            # Map productNumber -> Shopware ID
            found_count = 0
            for product in shopware_products:
                product_number = product.get("productNumber")
                shopware_id = product.get("id")

                if product_number and shopware_id and product_number in missing_codes:
                    # Add to existing products cache
                    self._existing_products[product_number] = shopware_id
                    found_count += 1

                    # Create missing Ecommerce Item mapping
                    try:
                        if not frappe.db.exists("Ecommerce Item", {
                            "integration": MODULE_NAME,
                            "erpnext_item_code": product_number
                        }):
                            parent_id = product.get("parentId")
                            frappe.get_doc({
                                "doctype": "Ecommerce Item",
                                "integration": MODULE_NAME,
                                "erpnext_item_code": product_number,
                                "integration_item_code": shopware_id,
                                "has_variants": 0 if parent_id else 1,
                                "sku": product_number,
                            }).insert(ignore_permissions=True)
                            self.logger.info(f"Created missing Ecommerce Item mapping for {product_number}")
                    except frappe.DuplicateEntryError:
                        pass
                    except Exception as e:
                        self.logger.warning(f"Could not create Ecommerce Item for {product_number}: {e}")

            if found_count > 0:
                self.logger.info(f"Reconciled {found_count} products from Shopware (missing ERPNext mappings)")

        except Exception as e:
            self.logger.warning(f"Shopware product reconciliation failed: {e}")

    def _process_batch(
        self,
        client,
        item_codes: List[str],
        skip_images: bool = False
    ) -> BatchUploadResult:
        """
        Process a batch of items using Shopware Sync API.

        Args:
            client: Shopware API client
            item_codes: Batch of item codes
            skip_images: Skip image sync

        Returns:
            BatchUploadResult for this batch
        """
        result = BatchUploadResult(total=len(item_codes))

        # Batch read items from ERPNext
        items_data = self._batch_read_items(item_codes)

        if not items_data:
            for ic in item_codes:
                result.skipped_items.append({"item_code": ic, "reason": "not_found_in_db"})
            result.skipped = len(item_codes)
            return result

        # Build sync payload
        upsert_payloads = []
        create_ecom_items = []

        for item_code in item_codes:
            item_data = items_data.get(item_code)
            if not item_data:
                result.skipped += 1
                result.skipped_items.append({"item_code": item_code, "reason": "not_found_in_batch_read"})
                continue

            try:
                payload, is_new, skip_reason = self._build_product_payload(client, item_data)
                if payload:
                    upsert_payloads.append({
                        "payload": payload,
                        "item_code": item_code,
                        "is_new": is_new
                    })

                    if is_new:
                        create_ecom_items.append({
                            "item_code": item_code,
                            "shopware_id": payload["id"]
                        })
                else:
                    result.skipped += 1
                    result.skipped_items.append({"item_code": item_code, "reason": skip_reason or "unknown"})

            except Exception as e:
                result.failed += 1
                result.errors.append({
                    "item_code": item_code,
                    "error": str(e)[:200]
                })

        if not upsert_payloads:
            return result

        # Track items needing visibility updates (existing products)
        visibility_updates = []

        # Track products with categories for batch sync (to remove old categories)
        products_with_categories = []
        
        # Extract actual payloads for API call, removing internal markers
        api_payloads = []
        for p in upsert_payloads:
            payload = p["payload"].copy()
            is_new = p.get("is_new", False)

            # Check if this product needs visibility update
            if payload.pop("_needs_visibility_update", False):
                item_code = payload.pop("_item_code", None)
                if item_code:
                    visibility_updates.append({
                        "product_id": payload["id"],
                        "item_code": item_code
                    })

            # Extract category IDs for batch category sync
            categories = payload.get("categories", [])
            if categories or not is_new:
                # Collect new category IDs (for comparison with existing)
                new_category_ids = {cat["id"] for cat in categories if "id" in cat}
                products_with_categories.append({
                    "product_id": payload["id"],
                    "new_category_ids": new_category_ids,
                    "is_new": is_new
                })

            api_payloads.append(payload)

        # IMPORTANT: Remove old categories BEFORE upserting new ones
        # (Shopware only ADDS categories, doesn't replace them)
        if products_with_categories:
            self._batch_sync_categories(client, products_with_categories)

        # Execute sync API call
        try:
            sync_payload = {
                "upsert-product": {
                    "entity": "product",
                    "action": "upsert",
                    "payload": api_payloads
                }
            }

            response = _sync_with_retry(client, sync_payload, self.logger, f"product upsert batch ({len(api_payloads)} items)")

            # Check for errors in response
            if response and response.get("success") is False:
                errors = response.get("data", {}).get("upsert-product", {}).get("result", [])
                for error in errors:
                    if error.get("errors"):
                        result.failed += 1
                        result.errors.append({
                            "item_code": "batch",
                            "error": str(error.get("errors"))[:200]
                        })
            else:
                result.success = len(upsert_payloads)

                # Track processed item codes and IDs
                for p in upsert_payloads:
                    result.processed_item_codes.append(p["item_code"])
                    if p["is_new"]:
                        result.created_ids.append(p["payload"]["id"])
                    else:
                        result.updated_ids.append(p["payload"]["id"])

            # Create Ecommerce Item records for new products
            for ecom_data in create_ecom_items:
                try:
                    frappe.get_doc({
                        "doctype": "Ecommerce Item",
                        "integration": MODULE_NAME,
                        "erpnext_item_code": ecom_data["item_code"],
                        "integration_item_code": ecom_data["shopware_id"],
                        "has_variants": 0,
                        "sku": ecom_data["item_code"],
                    }).insert(ignore_permissions=True)
                except frappe.DuplicateEntryError:
                    pass  # Already exists

            # Update visibilities for existing products (after successful batch sync)
            if visibility_updates:
                self._batch_update_visibilities(client, visibility_updates)

        except Exception as e:
            result.failed = len(upsert_payloads)
            result.success = 0
            result.errors.append({
                "item_code": "batch_sync",
                "error": str(e)[:500]
            })
            self.logger.error(f"Batch sync failed: {str(e)}", exception=e)

        return result

    def _batch_read_items(self, item_codes: List[str]) -> Dict[str, Dict]:
        """
        Read multiple items from ERPNext in a single query.

        Args:
            item_codes: List of item codes to read

        Returns:
            Dict mapping item_code to item data
        """
        if not item_codes:
            return {}

        # Get main item fields (including disabled - they sync as active=false)
        items = frappe.get_all(
            "Item",
            filters={"name": ["in", item_codes]},
            fields=[
                "name", "item_code", "item_name", "item_group",
                "description", "disabled", "has_variants", "variant_of",
                "weight_per_unit", "brand", "default_item_manufacturer",
                # SEO fields
                "ai_seo_title", "seo_title",
                "ai_seo_description", "seo_meta_description",
                "seo_keywords",
                # Description fields
                "ai_long_description",
                # AI Benefits and Custom Fields
                "ai_benefits",
                "ai_short_description",
                # Dimensions
                "item_height", "item_width", "item_length",
                # Custom selling rate field
                ITEM_SELLING_RATE_FIELD,
                # Delivery time
                "delivery_time",
            ]
        )

        items_dict = {item.name: dict(item) for item in items}

        # Skip templates and variants for now (handle separately)
        simple_items = {
            k: v for k, v in items_dict.items()
            if not v.get("has_variants") and not v.get("variant_of")
        }

        # Batch fetch prices
        if simple_items:
            self._batch_load_prices(list(simple_items.keys()), simple_items)

        # Batch fetch barcodes
        if simple_items:
            self._batch_load_barcodes(list(simple_items.keys()), simple_items)

        # Batch fetch tax templates
        if simple_items:
            self._batch_load_taxes(list(simple_items.keys()), simple_items)

        # Batch fetch shopware_properties (custom fields from child table)
        if simple_items:
            self._batch_load_shopware_properties(list(simple_items.keys()), simple_items)

        return simple_items

    def _batch_load_prices(self, item_codes: List[str], items_dict: Dict) -> None:
        """Load prices for multiple items."""
        if not item_codes:
            return

        # Get selling price list
        price_list = (
            getattr(self.setting, 'default_selling_price_list', None) or
            frappe.db.get_single_value("Selling Settings", "selling_price_list")
        )

        prices = frappe.get_all(
            "Item Price",
            filters={
                "item_code": ["in", item_codes],
                "selling": 1,
                "price_list": price_list
            },
            fields=["item_code", "price_list_rate"]
        )

        for price in prices:
            if price.item_code in items_dict:
                items_dict[price.item_code]["_price"] = flt(price.price_list_rate)

    def _batch_load_barcodes(self, item_codes: List[str], items_dict: Dict) -> None:
        """Load barcodes for multiple items."""
        if not item_codes:
            return

        barcodes = frappe.get_all(
            "Item Barcode",
            filters={"parent": ["in", item_codes]},
            fields=["parent", "barcode"],
            order_by="idx asc"
        )

        # Take first barcode per item
        seen = set()
        for bc in barcodes:
            if bc.parent not in seen and bc.parent in items_dict:
                items_dict[bc.parent]["_barcode"] = bc.barcode
                seen.add(bc.parent)

    def _batch_load_taxes(self, item_codes: List[str], items_dict: Dict) -> None:
        """Load tax rates for multiple items."""
        if not item_codes:
            return

        # Get tax templates for items
        taxes = frappe.get_all(
            "Item Tax",
            filters={"parent": ["in", item_codes]},
            fields=["parent", "item_tax_template"]
        )

        # Get rates from templates
        templates = set(t.item_tax_template for t in taxes if t.item_tax_template)
        template_rates = {}

        if templates:
            rates = frappe.get_all(
                "Item Tax Template Detail",
                filters={"parent": ["in", list(templates)]},
                fields=["parent", "tax_rate"]
            )
            for r in rates:
                template_rates[r.parent] = flt(r.tax_rate)

        # Apply to items
        for tax in taxes:
            if tax.parent in items_dict and tax.item_tax_template in template_rates:
                items_dict[tax.parent]["_tax_rate"] = template_rates[tax.item_tax_template]

    def _batch_load_shopware_properties(self, item_codes: List[str], items_dict: Dict) -> None:
        """
        Load shopware_properties child table for multiple items.

        This loads the flexible key-value properties from the 'Item Shopware Property'
        child table and stores them as '_shopware_properties' in each item dict.
        """
        if not item_codes:
            return

        try:
            # Query the child table for all items
            properties = frappe.get_all(
                "Item Shopware Property",
                filters={"parent": ["in", item_codes]},
                fields=["parent", "property_name", "property_value", "property_type"]
            )

            # Group by parent
            for prop in properties:
                parent = prop.parent
                if parent in items_dict:
                    if "_shopware_properties" not in items_dict[parent]:
                        items_dict[parent]["_shopware_properties"] = []
                    items_dict[parent]["_shopware_properties"].append({
                        "property_name": prop.property_name,
                        "property_value": prop.property_value,
                        "property_type": prop.property_type
                    })
        except Exception:
            # Child table may not exist in all installations
            pass

    def _build_product_payload(
        self,
        client,
        item_data: Dict
    ) -> Tuple[Optional[Dict], bool, Optional[str]]:
        """
        Build Shopware product payload from ERPNext item data.

        Args:
            client: Shopware API client
            item_data: Item data dict from batch read

        Returns:
            Tuple of (payload dict, is_new_product, skip_reason)
            skip_reason is set when payload is None
        """
        item_code = item_data["name"]

        # Check if exists
        existing_id = self._existing_products.get(item_code)
        is_new = not existing_id
        product_id = existing_id or generate_uuid(f"product_{item_code}")

        # Get price
        price = flt(item_data.get("_price") or item_data.get(ITEM_SELLING_RATE_FIELD) or 0)
        if price <= 0:
            # Skip items without price
            return None, False, "no_price"

        # Tax rate
        tax_rate = flt(item_data.get("_tax_rate") or 19.0)
        gross_price = round(price * (1 + tax_rate / 100), 2)

        # Get tax ID (cached)
        if tax_rate not in self._tax_ids:
            self._tax_ids[tax_rate] = self._get_tax_id(client, tax_rate)
        tax_id = self._tax_ids.get(tax_rate)

        # Description
        description = (
            item_data.get("ai_long_description") or
            item_data.get("description") or
            item_data.get("item_name")
        )

        # Check for listPrice (Streichpreis/UVP)
        from ecommerce_integrations.shopware6.export.price_handler import _get_list_price_payload
        list_price = _get_list_price_payload(item_code, price, tax_rate, self._currency_id)

        price_entry = {
            "currencyId": self._currency_id,
            "gross": gross_price,
            "net": price,
            "linked": False,
        }
        if list_price:
            price_entry["listPrice"] = list_price

        payload = {
            "id": product_id,
            "productNumber": item_code,
            "name": item_data.get("item_name") or item_code,
            "description": description,
            "active": not item_data.get("disabled"),
            "stock": 0,
            "isCloseout": False,
            "price": [price_entry],
        }

        if tax_id:
            payload["taxId"] = tax_id

        # Manufacturer
        manufacturer = item_data.get("brand") or item_data.get("default_item_manufacturer")
        if manufacturer:
            mfr_id = self._get_manufacturer_id(client, manufacturer)
            if mfr_id:
                payload["manufacturerId"] = mfr_id

        # SEO fields
        seo_title = item_data.get("ai_seo_title") or item_data.get("seo_title")
        seo_desc = item_data.get("ai_seo_description") or item_data.get("seo_meta_description")

        if seo_title:
            payload["metaTitle"] = seo_title
        if seo_desc:
            payload["metaDescription"] = seo_desc
        if item_data.get("seo_keywords"):
            payload["keywords"] = item_data["seo_keywords"]

        # Barcode
        if item_data.get("_barcode"):
            payload["ean"] = item_data["_barcode"]

        # Weight
        if item_data.get("weight_per_unit"):
            payload["weight"] = flt(item_data["weight_per_unit"])

        # Dimensions (cm to mm)
        if item_data.get("item_height"):
            payload["height"] = flt(item_data["item_height"]) * 10
        if item_data.get("item_width"):
            payload["width"] = flt(item_data["item_width"]) * 10
        if item_data.get("item_length"):
            payload["length"] = flt(item_data["item_length"]) * 10

        # Visibility - Multi-Channel support
        # ALWAYS handle visibilities separately after batch sync to avoid duplicate key errors
        # (Product might exist in Shopware even if not in ERPNext mappings)
        payload["_needs_visibility_update"] = True
        payload["_item_code"] = item_code

        # Properties from shopware_properties table (type "Property" becomes filter properties)
        from ecommerce_integrations.shopware6.export.property_handler import (
            get_or_create_property_group,
            get_or_create_property_option,
        )

        shopware_properties = item_data.get("_shopware_properties") or []
        property_ids = []
        for prop in shopware_properties:
            prop_type = prop.get("property_type")
            if prop_type in ("Property", "Text") and prop.get("property_value"):
                group_id = get_or_create_property_group(client, prop["property_name"])
                if group_id:
                    option_id = get_or_create_property_option(
                        client, group_id, prop["property_name"], prop["property_value"]
                    )
                    if option_id:
                        property_ids.append({"id": option_id})

        if property_ids:
            payload["properties"] = property_ids

        # Custom Fields (AI Benefits, Short Description, Zubehoer, etc.)
        custom_fields = self._build_custom_fields(item_data)
        if custom_fields:
            payload["customFields"] = custom_fields

        # Categories - lookup category IDs (categories already bulk-synced in Phase 1)
        # Using skip_sync=True for fast lookup only - no redundant API calls
        try:
            from ecommerce_integrations.shopware6.export.category_handler import sync_all_item_categories
            category_refs = sync_all_item_categories(client, item_code, skip_sync=True)
            if category_refs:
                payload["categories"] = category_refs
                self.logger.debug(f"Assigned {len(category_refs)} categories to {item_code}")
        except Exception as cat_err:
            self.logger.warning(f"Category sync failed for {item_code}: {cat_err}")

        # Delivery Time
        delivery_time_str = item_data.get("delivery_time")
        if delivery_time_str:
            delivery_time_id = self._get_delivery_time_id(client, delivery_time_str)
            if delivery_time_id:
                payload["deliveryTimeId"] = delivery_time_id

        return payload, is_new, None  # No skip reason - success

    def _get_currency_id(self, client, code: str = "EUR") -> Optional[str]:
        """Get currency ID with caching."""
        cached = self.cache.get_currency_id(code)
        if cached:
            return cached

        try:
            response = client.request_post(
                "search/currency",
                {"filter": [{"type": "equals", "field": "isoCode", "value": code}], "limit": 1}
            )
            currencies = response.get("data", [])
            if currencies:
                currency_id = currencies[0]["id"]
                self.cache.set_currency_id(code, currency_id)
                return currency_id
        except Exception:
            pass
        return None

    def _get_tax_id(self, client, rate: float) -> Optional[str]:
        """Get tax ID with caching."""
        rate = round(rate, 2)
        cached = self.cache.get("tax", str(rate))
        if cached:
            return cached

        try:
            response = client.request_post(
                "search/tax",
                {"filter": [{"type": "equals", "field": "taxRate", "value": rate}], "limit": 1}
            )
            taxes = response.get("data", [])
            if taxes:
                tax_id = taxes[0]["id"]
                self.cache.set("tax", str(rate), tax_id)
                return tax_id
        except Exception:
            pass
        return None

    def _get_manufacturer_id(self, client, name: str) -> Optional[str]:
        """Get or create manufacturer with caching."""
        if not name:
            return None

        # Check local cache first
        if name in self._manufacturer_ids:
            return self._manufacturer_ids[name]

        # Check persistent cache
        cached = self.cache.get("manufacturer", name)
        if cached:
            self._manufacturer_ids[name] = cached
            return cached

        try:
            response = client.request_post(
                "search/product-manufacturer",
                {"filter": [{"type": "equals", "field": "name", "value": name}], "limit": 1}
            )
            manufacturers = response.get("data", [])

            if manufacturers:
                mfr_id = manufacturers[0]["id"]
            else:
                # Create
                mfr_id = generate_uuid(f"manufacturer_{name}")
                client.request_post("product-manufacturer", {"id": mfr_id, "name": name})

            self.cache.set("manufacturer", name, mfr_id)
            self._manufacturer_ids[name] = mfr_id
            return mfr_id

        except Exception:
            return None


    def _batch_update_visibilities(self, client, visibility_updates: List[Dict]) -> None:
        """
        BATCH update product visibilities using Shopware Sync API.

        OPTIMIZED: Uses bulk operations instead of individual API calls.
        - 1 bulk search to get current visibilities for all products
        - 1 Sync API call to upsert all new/updated visibilities
        - 1 Sync API call to delete removed visibilities

        Args:
            client: Shopware API client
            visibility_updates: List of dicts with product_id and item_code
        """
        if not visibility_updates:
            return

        self.logger.info(f"[BATCH] Updating visibilities for {len(visibility_updates)} products")

        # Step 1: Get all product IDs
        product_ids = [u["product_id"] for u in visibility_updates]

        # Step 2: Bulk fetch current visibilities from Shopware (paginated)
        current_visibilities = {}  # product_id -> {channel_id: {id, visibility}}
        page = 1
        while True:
            try:
                search_payload = {
                    "filter": [{"type": "equalsAny", "field": "productId", "value": product_ids}],
                    "limit": 500,
                    "page": page
                }
                response = client.request_post("search/product-visibility", search_payload)
                vis_data = response.get("data", [])

                if not vis_data:
                    break

                for v in vis_data:
                    pid = v.get("productId")
                    if pid not in current_visibilities:
                        current_visibilities[pid] = {}
                    current_visibilities[pid][v.get("salesChannelId")] = {
                        "id": v.get("id"),
                        "visibility": v.get("visibility")
                    }

                if len(vis_data) < 500:
                    break
                page += 1
            except Exception as e:
                self.logger.warning(f"Failed to fetch current visibilities: {e}")
                break

        # Step 3: Calculate desired visibilities for each product
        upsert_payloads = []  # Visibilities to create/update
        delete_payloads = []  # Visibilities to delete

        default_channel_id = self._get_default_sales_channel_id(client)

        for update in visibility_updates:
            product_id = update["product_id"]
            item_code = update["item_code"]
            variant_of = update.get("variant_of")

            try:
                # Get desired visibilities from ERPNext
                # For variants: use parent item for visibility (consistent with variant_handler.py)
                if variant_of:
                    visibility_item = frappe.get_doc("Item", variant_of)
                else:
                    visibility_item = frappe.get_doc("Item", item_code)
                new_visibilities = get_product_visibilities(visibility_item, self.setting)

                if not new_visibilities and default_channel_id:
                    new_visibilities = [{"salesChannelId": default_channel_id, "visibility": 30}]

                if not new_visibilities:
                    continue

                current = current_visibilities.get(product_id, {})
                new_by_channel = {v["salesChannelId"]: v["visibility"] for v in new_visibilities}

                # Find visibilities to delete (in current but not in new)
                for channel_id, vis_data in current.items():
                    if channel_id not in new_by_channel:
                        delete_payloads.append({"id": vis_data["id"]})

                # Find visibilities to upsert (new or changed)
                for channel_id, visibility in new_by_channel.items():
                    if channel_id in current:
                        # Update only if visibility level changed
                        if current[channel_id]["visibility"] != visibility:
                            upsert_payloads.append({
                                "id": current[channel_id]["id"],
                                "productId": product_id,
                                "salesChannelId": channel_id,
                                "visibility": visibility
                            })
                    else:
                        # Create new - generate deterministic ID
                        vis_id = generate_uuid(f"vis_{product_id}_{channel_id}")
                        upsert_payloads.append({
                            "id": vis_id,
                            "productId": product_id,
                            "salesChannelId": channel_id,
                            "visibility": visibility
                        })

            except Exception as e:
                self.logger.warning(f"Failed to calculate visibilities for {item_code}: {e}")

        # Step 4: Execute batch operations via Sync API
        sync_payload = {}

        if upsert_payloads:
            self.logger.info(f"[BATCH] Upserting {len(upsert_payloads)} visibilities")
            sync_payload["upsert-visibilities"] = {
                "entity": "product_visibility",
                "action": "upsert",
                "payload": upsert_payloads
            }

        if delete_payloads:
            self.logger.info(f"[BATCH] Deleting {len(delete_payloads)} visibilities")
            sync_payload["delete-visibilities"] = {
                "entity": "product_visibility",
                "action": "delete",
                "payload": delete_payloads
            }

        if sync_payload:
            try:
                _sync_with_retry(client, sync_payload, self.logger, f"visibility batch ({len(upsert_payloads)} upsert, {len(delete_payloads)} delete)")
                self.logger.info("[BATCH] Visibility update completed successfully")
            except Exception as e:
                self.logger.error(f"[BATCH] Visibility sync failed: {e}")

    def _batch_sync_categories(
        self,
        client,
        products_with_categories: List[Dict]
    ) -> None:
        """
        BATCH sync product categories - only delete removed categories.

        OPTIMIZED: Uses bulk operations instead of clearing all categories.
        - 1 bulk search to get current categories for all products
        - Compares with new categories to find removals
        - 1 Sync API call to delete only removed categories

        This ensures that when a product's categories change in ERPNext,
        old categories are properly removed in Shopware.

        Args:
            client: Shopware API client
            products_with_categories: List of dicts with:
                - product_id: Shopware product UUID
                - new_category_ids: Set of new category IDs from ERPNext
                - is_new: Whether this is a new product (skip category removal)
        """
        if not products_with_categories:
            return

        # Filter to only existing products (new products don't need category removal)
        existing_products = [p for p in products_with_categories if not p.get("is_new")]

        if not existing_products:
            return

        product_ids = [p["product_id"] for p in existing_products]
        self.logger.info(f"[BATCH] Syncing categories for {len(product_ids)} existing products")

        # Step 1: Bulk fetch current categories from Shopware
        # NOTE: product_category is not directly searchable in Shopware 6
        # We must use search/product with categories association instead
        current_categories = {}  # product_id -> set of category_ids

        # Batch products in chunks of 100 for API efficiency
        for i in range(0, len(product_ids), 100):
            batch_ids = product_ids[i:i + 100]
            try:
                search_payload = {
                    "filter": [{"type": "equalsAny", "field": "id", "value": batch_ids}],
                    "associations": {"categories": {}},
                    "limit": 100
                }
                response = client.request_post("search/product", search_payload)
                products = response.get("data", [])

                for product in products:
                    pid = product.get("id")
                    categories = product.get("categories", [])
                    if pid:
                        current_categories[pid] = {c.get("id") for c in categories if c.get("id")}
            except Exception as e:
                self.logger.warning(f"Failed to fetch current categories for batch: {e}")

        # Step 2: Calculate categories to delete (in current but not in new)
        delete_payloads = []

        for product_info in existing_products:
            product_id = product_info["product_id"]
            new_category_ids = product_info.get("new_category_ids", set())

            current = current_categories.get(product_id, set())

            # Find categories to delete (in current but not in new)
            to_delete = current - new_category_ids

            if to_delete:
                self.logger.debug(
                    f"Product {product_id}: removing {len(to_delete)} categories, "
                    f"keeping {len(current & new_category_ids)}, adding {len(new_category_ids - current)}"
                )
                for cat_id in to_delete:
                    delete_payloads.append({
                        "productId": product_id,
                        "categoryId": cat_id
                    })

        # Step 3: Execute batch delete via Sync API
        if delete_payloads:
            self.logger.info(f"[BATCH] Deleting {len(delete_payloads)} removed category assignments")
            try:
                sync_payload = {
                    "delete-product-categories": {
                        "entity": "product_category",
                        "action": "delete",
                        "payload": delete_payloads
                    }
                }
                _sync_with_retry(
                    client, sync_payload, self.logger,
                    f"category delete batch ({len(delete_payloads)} removals)"
                )
                self.logger.info("[BATCH] Category removal completed successfully")
            except Exception as e:
                # Log but don't fail the whole batch - categories will just accumulate
                self.logger.warning(f"[BATCH] Category removal failed (non-fatal): {e}")
        else:
            self.logger.debug("[BATCH] No category removals needed")

    def _get_default_sales_channel_id(self, client) -> Optional[str]:
        """Get default sales channel ID."""
        # Check setting first
        for sc in (self.setting.sales_channels or []):
            if sc.is_default and sc.active:
                return sc.sales_channel_id

        # Fallback to first active
        for sc in (self.setting.sales_channels or []):
            if sc.active:
                return sc.sales_channel_id

        # Ultimate fallback
        cached = self.cache.get_sales_channel_id()
        if cached:
            return cached

        try:
            response = client.request_get("sales-channel")
            channels = response.get("data", [])
            if channels:
                sc_id = channels[0]["id"]
                self.cache.set_sales_channel_id(sc_id)
                return sc_id
        except Exception:
            pass

        return None

    def _get_delivery_time_id(self, client, delivery_time_name: str) -> Optional[str]:
        """
        Get or create delivery time ID with caching.

        Args:
            client: Shopware API client
            delivery_time_name: Name of the delivery time (e.g., "3-5 Werktage")

        Returns:
            Shopware delivery time ID
        """
        if not delivery_time_name:
            return None

        # Check cache first
        cached = self.cache.get("delivery_time", delivery_time_name)
        if cached:
            return cached

        # Use the existing function from product_mapper
        from ecommerce_integrations.shopware6.export.product_mapper import get_or_create_delivery_time
        delivery_time_id = get_or_create_delivery_time(client, delivery_time_name)

        return delivery_time_id

    def _build_custom_fields(self, item_data: Dict) -> Dict[str, Any]:
        """
        Build Shopware custom fields dict from ERPNext item data.

        Maps ERPNext fields to Shopware custom field names using:
        1. PRODUCT_CUSTOM_FIELDS_MAP (hardcoded mappings)
        2. shopware_properties child table (flexible key-value pairs)

        Args:
            item_data: Item data dict from batch read

        Returns:
            Dict with Shopware custom field names as keys
        """
        from frappe.utils import cstr

        custom_fields = {}

        # Source 1: Hardcoded mappings (AI fields, Zubehoer, etc.)
        for erpnext_field, shopware_field in PRODUCT_CUSTOM_FIELDS_MAP.items():
            value = item_data.get(erpnext_field)
            if value:
                custom_fields[shopware_field] = cstr(value).strip()

        # Source 2: shopware_properties child table (flexible key-value)
        properties = item_data.get("_shopware_properties") or []
        for prop in properties:
            if prop.get("property_type") == "Custom Field" and prop.get("property_value"):
                # Generate Shopware field name from property name
                shopware_field_name = f"erpnext_{prop['property_name'].lower().replace(' ', '_')}"
                value = cstr(prop["property_value"]).strip()

                # Convert boolean strings to actual booleans
                if value.lower() in ('true', '1', 'yes'):
                    custom_fields[shopware_field_name] = True
                elif value.lower() in ('false', '0', 'no'):
                    custom_fields[shopware_field_name] = False
                else:
                    custom_fields[shopware_field_name] = value

        return custom_fields

    # =========================================================================
    # BATCH TEMPLATE UPLOAD
    # =========================================================================

    def upload_templates(self, item_codes: List[str], skip_images: bool = False, start_batch: int = 1) -> BatchUploadResult:
        """
        Upload multiple template items (has_variants=1) to Shopware in batches.
        Uses Sync API for bulk upsert with configuratorSettings.

        Args:
            item_codes: List of ERPNext template item codes
            skip_images: Skip image sync for faster upload
            start_batch: Start from this batch number (1-based, for resuming interrupted syncs)

        Returns:
            BatchUploadResult with statistics
        """
        from ecommerce_integrations.shopware6.connection import get_shopware_client
        from ecommerce_integrations.shopware6.export.property_handler import (
            get_or_create_property_group,
            get_or_create_variant_option,
        )

        result = BatchUploadResult(total=len(item_codes))

        if not item_codes:
            return result

        client = get_shopware_client()
        if not client:
            self.logger.error("Could not get Shopware client")
            return result

        # Initialize caches
        self._init_caches(client)

        # Validate critical cache values - currency_id is required for price payloads
        if not self._currency_id:
            self.logger.error("Failed to get EUR currency ID from Shopware - cannot sync templates without valid currency")
            result.failed = len(item_codes)
            result.errors.append({
                "item_code": "INIT",
                "error": "Currency ID lookup failed - ensure EUR currency exists in Shopware"
            })
            return result

        # Load existing mappings
        self._load_existing_mappings(item_codes)

        # Reconcile with Shopware - find products that exist in Shopware but not in ERPNext mappings
        self._reconcile_with_shopware(client, item_codes)

        # Pre-cache property groups and options for all templates
        self._property_group_cache: Dict[str, str] = {}
        self._option_cache: Dict[str, str] = {}

        # Process in batches
        total_batches = (len(item_codes) + BATCH_SIZE - 1) // BATCH_SIZE

        # Calculate starting position (start_batch is 1-based)
        start_index = (start_batch - 1) * BATCH_SIZE if start_batch > 1 else 0
        if start_batch > 1:
            self.logger.info(f"Resuming templates from batch {start_batch}/{total_batches} (skipping first {start_index} items)")

        for batch_start in range(start_index, len(item_codes), BATCH_SIZE):
            batch_codes = item_codes[batch_start:batch_start + BATCH_SIZE]
            batch_num = batch_start // BATCH_SIZE + 1

            try:
                batch_result = self._process_template_batch(client, batch_codes, skip_images)

                result.success += batch_result.success
                result.failed += batch_result.failed
                result.skipped += batch_result.skipped
                result.errors.extend(batch_result.errors)
                result.created_ids.extend(batch_result.created_ids)
                result.updated_ids.extend(batch_result.updated_ids)
                result.processed_item_codes.extend(batch_result.processed_item_codes)
                result.skipped_items.extend(batch_result.skipped_items)

                frappe.db.commit()

                processed = min(batch_start + BATCH_SIZE, len(item_codes))
                synced_codes = ", ".join(batch_result.processed_item_codes[:5])
                if len(batch_result.processed_item_codes) > 5:
                    synced_codes += f"... (+{len(batch_result.processed_item_codes) - 5} more)"
                self.logger.info(
                    f"Template Batch {batch_num}/{total_batches}: "
                    f"{batch_result.success} success, {batch_result.failed} failed, {batch_result.skipped} skipped. "
                    f"Items: {synced_codes}"
                )

                # Log to Ecommerce Integration Log (like category sync does)
                batch_summary = (
                    f"Templates Batch {batch_num}/{total_batches}: "
                    f"{batch_result.success} success, {batch_result.failed} failed, {batch_result.skipped} skipped"
                )
                create_shopware_log(
                    status="Success" if batch_result.failed == 0 else "Partial Success",
                    method="BatchProductUploader.upload_templates.batch",
                    message=batch_summary,
                    request_data={
                        "batch_num": batch_num,
                        "total_batches": total_batches,
                        "batch_size": len(batch_codes),
                        "success": batch_result.success,
                        "failed": batch_result.failed,
                        "skipped": batch_result.skipped,
                        "items": batch_result.processed_item_codes[:20],
                        "errors": batch_result.errors[:5] if batch_result.errors else [],
                        "skipped_items": batch_result.skipped_items[:10] if batch_result.skipped_items else []
                    }
                )

                if self.progress_callback:
                    try:
                        self.progress_callback(
                            batch_num, total_batches, processed, len(item_codes),
                            result.success, result.failed
                        )
                    except Exception as e:
                        self.logger.warning(f"Progress callback failed: {e}")

            except Exception as batch_error:
                # Log error but CONTINUE with next batch instead of aborting
                error_msg = str(batch_error)[:500]
                self.logger.error(
                    f"Template Batch {batch_num}/{total_batches} FAILED with error: {error_msg}. "
                    f"Continuing with next batch..."
                )
                result.failed += len(batch_codes)
                for item_code in batch_codes:
                    result.errors.append({"item_code": item_code, "error": f"Batch failed: {error_msg[:100]}"})

                # Log batch failure
                create_shopware_log(
                    status="Error",
                    method="BatchProductUploader.upload_templates.batch",
                    message=f"Templates Batch {batch_num}/{total_batches} FAILED: {error_msg[:200]}",
                    request_data={
                        "batch_num": batch_num,
                        "total_batches": total_batches,
                        "batch_size": len(batch_codes),
                        "items": batch_codes[:20],
                        "error": error_msg
                    }
                )

                # Try to commit any partial work and reconnect if needed
                try:
                    frappe.db.commit()
                except Exception:
                    try:
                        frappe.db.rollback()
                        frappe.connect()
                    except Exception:
                        pass

        return result

    def _process_template_batch(
        self,
        client,
        item_codes: List[str],
        skip_images: bool = False
    ) -> BatchUploadResult:
        """Process a batch of template items using Shopware Sync API."""
        # Note: property_handler imports moved to _get_property_group_id/_get_property_option_id (fallback only)
        from ecommerce_integrations.shopware6.export.category_handler import sync_all_item_categories

        result = BatchUploadResult(total=len(item_codes))
        self.logger.info(f"[TIMING] _process_template_batch START for {len(item_codes)} items")

        # Batch read templates from ERPNext
        self.logger.info("[TIMING] _batch_read_templates START")
        templates_data = self._batch_read_templates(item_codes)
        self.logger.info(f"[TIMING] _batch_read_templates DONE - {len(templates_data)} items read")

        if not templates_data:
            for ic in item_codes:
                result.skipped_items.append({"item_code": ic, "reason": "not_found_in_db"})
            result.skipped = len(item_codes)
            return result

        # OPTIMIZATION: Bulk prefetch all property groups and options (3 API calls instead of 1000+)
        self._bulk_prefetch_property_groups_and_options(client, templates_data)

        upsert_payloads = []
        create_ecom_items = []
        existing_product_ids = []  # Track existing products for configurator cleanup

        self.logger.info("[TIMING] Building payloads START")
        for item_code in item_codes:
            item_data = templates_data.get(item_code)
            if not item_data:
                result.skipped += 1
                result.skipped_items.append({"item_code": item_code, "reason": "not_found_in_batch_read"})
                continue

            try:
                # Build template payload
                payload, is_new, skip_reason = self._build_template_payload(client, item_data)
                if payload:
                    upsert_payloads.append({
                        "payload": payload,
                        "item_code": item_code,
                        "is_new": is_new
                    })
                    if is_new:
                        create_ecom_items.append({
                            "item_code": item_code,
                            "shopware_id": payload["id"]
                        })
                    else:
                        # Track existing products for configurator cleanup
                        existing_product_ids.append(payload["id"])
                else:
                    result.skipped += 1
                    result.skipped_items.append({"item_code": item_code, "reason": skip_reason or "unknown"})

            except Exception as e:
                result.failed += 1
                result.errors.append({
                    "item_code": item_code,
                    "error": str(e)[:200]
                })

        self.logger.info(f"[TIMING] Building payloads DONE - {len(upsert_payloads)} payloads built")

        if not upsert_payloads:
            return result

        # OPTIMIZED: Bulk clear configuratorSettings using Sync API (much faster than sequential deletes)
        if existing_product_ids:
            self.logger.info(f"[TIMING] Bulk clear configuratorSettings START for {len(existing_product_ids)} products")
            failed_product_ids = self._bulk_clear_configurator_settings(client, existing_product_ids)
            self.logger.info("[TIMING] Bulk clear configuratorSettings DONE")

            # Remove products with failed cleanup from the batch to avoid duplicate key errors
            if failed_product_ids:
                original_count = len(upsert_payloads)
                upsert_payloads = [
                    p for p in upsert_payloads
                    if p["payload"]["id"] not in failed_product_ids
                ]
                excluded_count = original_count - len(upsert_payloads)
                if excluded_count > 0:
                    self.logger.info(f"Excluded {excluded_count} products from batch due to configuratorSettings cleanup failure")
                    result.failed += excluded_count
                    result.errors.append({
                        "item_code": "configurator_cleanup",
                        "error": f"{excluded_count} products excluded due to configuratorSettings cleanup failure"
                    })

        if not upsert_payloads:
            self.logger.warning("No products left to sync after configuratorSettings cleanup")
            return result

        # Track templates needing visibility updates (existing templates)
        visibility_updates = []

        # Track templates with categories for batch sync (to remove old categories)
        products_with_categories = []
        
        # Extract actual payloads for API call, removing internal markers
        api_payloads = []
        for p in upsert_payloads:
            payload = p["payload"].copy()
            is_new = p.get("is_new", False)

            # Check if this template needs visibility update
            if payload.pop("_needs_visibility_update", False):
                item_code = payload.pop("_item_code", None)
                if item_code:
                    visibility_updates.append({
                        "product_id": payload["id"],
                        "item_code": item_code
                    })

            # Extract category IDs for batch category sync
            categories = payload.get("categories", [])
            if categories or not is_new:
                new_category_ids = {cat["id"] for cat in categories if "id" in cat}
                products_with_categories.append({
                    "product_id": payload["id"],
                    "new_category_ids": new_category_ids,
                    "is_new": is_new
                })

            api_payloads.append(payload)

        # IMPORTANT: Remove old categories BEFORE upserting new ones
        # (Shopware only ADDS categories, doesn't replace them)
        if products_with_categories:
            self._batch_sync_categories(client, products_with_categories)

        try:
            sync_payload = {
                "upsert-templates": {
                    "entity": "product",
                    "action": "upsert",
                    "payload": api_payloads
                }
            }

            _sync_with_retry(client, sync_payload, self.logger, f"template upsert batch ({len(api_payloads)} items)")

            # Success - update counts
            result.success = len(upsert_payloads)
            result.processed_item_codes = [p["item_code"] for p in upsert_payloads]
            result.created_ids = [p["payload"]["id"] for p in upsert_payloads if p["is_new"]]
            result.updated_ids = [p["payload"]["id"] for p in upsert_payloads if not p["is_new"]]

            # Create Ecommerce Item records
            for ecom_data in create_ecom_items:
                try:
                    frappe.get_doc({
                        "doctype": "Ecommerce Item",
                        "integration": MODULE_NAME,
                        "erpnext_item_code": ecom_data["item_code"],
                        "integration_item_code": ecom_data["shopware_id"],
                        "has_variants": 1,
                        "sku": ecom_data["item_code"],
                    }).insert(ignore_permissions=True)
                except frappe.DuplicateEntryError:
                    pass

            # Update visibilities for existing templates (after successful batch sync)
            if visibility_updates:
                self._batch_update_visibilities(client, visibility_updates)

        except BaseException as e:
            # Note: ShopwareAPIError inherits from BaseException
            result.failed = len(upsert_payloads)
            result.success = 0
            result.errors.append({
                "item_code": "batch_template_sync",
                "error": str(e)[:500]
            })
            self.logger.error(f"Template batch sync failed: {str(e)}")

        return result

    def _bulk_clear_configurator_settings(self, client, product_ids: List[str]) -> set:
        """
        Bulk clear configuratorSettings for multiple products using Sync API.

        OPTIMIZATION: Instead of N×M individual DELETE requests (N products × M settings each),
        this uses 1 search + 1 bulk delete = 2 API calls total.

        Args:
            client: Shopware API client
            product_ids: List of Shopware product UUIDs

        Returns:
            Set of product IDs that failed cleanup (should be excluded from batch)
        """
        failed_product_ids = set()

        if not product_ids:
            return failed_product_ids

        try:
            # Step 1: Batch fetch all configurator settings (paginated, max 500 per request)
            all_setting_ids = []
            page = 1
            while True:
                search_payload = {
                    "filter": [
                        {
                            "type": "equalsAny",
                            "field": "productId",
                            "value": product_ids
                        }
                    ],
                    "limit": 500,
                    "page": page
                }
                response = client.request_post("search/product-configurator-setting", search_payload)
                settings_data = response.get("data", [])
                if not settings_data:
                    break
                all_setting_ids.extend([s.get("id") for s in settings_data if s.get("id")])
                if len(settings_data) < 500:
                    break
                page += 1

            if not all_setting_ids:
                self.logger.info(f"No configuratorSettings to clear for {len(product_ids)} products")
                return failed_product_ids

            self.logger.info(f"Bulk clearing {len(all_setting_ids)} configuratorSettings for {len(product_ids)} products")

            # Step 2: Bulk delete all settings using Sync API (1 API call)
            delete_payload = {
                "delete-configurator-settings": {
                    "entity": "product_configurator_setting",
                    "action": "delete",
                    "payload": [{"id": sid} for sid in all_setting_ids]
                }
            }

            _sync_with_retry(client, delete_payload, self.logger, f"configurator settings delete ({len(all_setting_ids)} items)")
            self.logger.info(f"Successfully bulk-deleted {len(all_setting_ids)} configuratorSettings")

        except Exception as e:
            # On bulk failure, fall back to marking all as failed
            # The caller will exclude these from the batch
            self.logger.warning(f"Bulk configuratorSettings cleanup failed: {e}")
            # Don't fail all products - try to continue without cleanup
            # The upsert might still work if settings haven't changed

        return failed_product_ids

    def _bulk_prefetch_property_groups_and_options(
        self,
        client,
        templates_data: Dict[str, Dict]
    ) -> None:
        """
        Bulk prefetch and create property groups and options for all templates.

        OPTIMIZATION: Instead of N×M individual API calls (N templates × M attributes),
        this uses 2 search + 1 bulk upsert = 3 API calls total.

        Args:
            client: Shopware API client
            templates_data: Dict of template data with _attributes
        """
        # Reset caches for this batch
        self._property_groups = {}
        self._property_options = {}

        # Collect all unique attribute names and values
        all_group_names = set()
        all_options = {}  # group_name -> set of option values

        for item_data in templates_data.values():
            attributes = item_data.get("_attributes", [])
            for attr in attributes:
                group_name = attr.get("name")
                if group_name:
                    all_group_names.add(group_name)
                    if group_name not in all_options:
                        all_options[group_name] = set()
                    for val in attr.get("values", []):
                        option_value = val.get("value")
                        if option_value:
                            all_options[group_name].add(option_value)

        if not all_group_names:
            return

        self.logger.info(f"[TIMING] Bulk prefetch START: {len(all_group_names)} groups, "
                         f"{sum(len(v) for v in all_options.values())} options")

        try:
            # Step 1: Fetch ALL property groups from Shopware (1 API call)
            groups_response = client.request_post("search/property-group", {
                "limit": 500,
                "includes": {"property_group": ["id", "name"]}
            })

            existing_groups = {}
            for g in groups_response.get("data", []):
                existing_groups[g.get("name")] = g.get("id")

            # Identify missing groups
            missing_groups = []
            for group_name in all_group_names:
                if group_name in existing_groups:
                    self._property_groups[group_name] = existing_groups[group_name]
                else:
                    # Generate ID for new group
                    group_id = generate_uuid(f"property_group_{group_name}")
                    self._property_groups[group_name] = group_id
                    missing_groups.append({
                        "id": group_id,
                        "name": group_name,
                        "displayType": "text",
                        "sortingType": "alphanumeric",
                        "filterable": True,
                        "visibleOnProductDetailPage": True,
                    })

            self.logger.info(f"Property groups: {len(existing_groups)} existing, "
                             f"{len(missing_groups)} to create")

            # Step 2: Fetch ALL property options from Shopware (paginated, max 500 per request)
            # Build reverse lookup: groupId -> name
            group_id_to_name = {v: k for k, v in self._property_groups.items()}
            existing_options = {}  # "group_name:option_value" -> option_id

            page = 1
            while True:
                options_response = client.request_post("search/property-group-option", {
                    "limit": 500,
                    "page": page,
                    "includes": {"property_group_option": ["id", "name", "groupId"]}
                })
                data = options_response.get("data", [])
                if not data:
                    break
                for opt in data:
                    group_id = opt.get("groupId")
                    option_name = opt.get("name")
                    if group_id in group_id_to_name and option_name:
                        group_name = group_id_to_name[group_id]
                        key = f"{group_name}:{option_name}"
                        existing_options[key] = opt.get("id")
                if len(data) < 500:
                    break
                page += 1

            # Identify missing options
            missing_options = []
            for group_name, option_values in all_options.items():
                group_id = self._property_groups.get(group_name)
                if not group_id:
                    continue

                for option_value in option_values:
                    key = f"{group_name}:{option_value}"
                    if key in existing_options:
                        self._property_options[key] = existing_options[key]
                    else:
                        # Generate ID for new option
                        option_id = generate_uuid(f"property_option_{group_name}_{option_value}")
                        self._property_options[key] = option_id
                        missing_options.append({
                            "id": option_id,
                            "groupId": group_id,
                            "name": option_value,
                        })

            self.logger.info(f"Property options: {len(existing_options)} existing, "
                             f"{len(missing_options)} to create")

            # Step 3: Bulk create missing groups and options (1 API call)
            if missing_groups or missing_options:
                sync_payload = {}

                if missing_groups:
                    sync_payload["create-property-groups"] = {
                        "entity": "property_group",
                        "action": "upsert",
                        "payload": missing_groups
                    }

                if missing_options:
                    sync_payload["create-property-options"] = {
                        "entity": "property_group_option",
                        "action": "upsert",
                        "payload": missing_options
                    }

                _sync_with_retry(client, sync_payload, self.logger, f"property groups/options create ({len(missing_groups)} groups, {len(missing_options)} options)")
                self.logger.info(f"Created {len(missing_groups)} groups, {len(missing_options)} options")

            # Also update Redis cache for future use
            cache = get_cache()
            for group_name, group_id in self._property_groups.items():
                cache.set_property_group_id(group_name, group_id)
            for key, option_id in self._property_options.items():
                group_name, option_value = key.split(":", 1)
                cache.set_property_option_id(group_name, option_value, option_id)

            self.logger.info("[TIMING] Bulk prefetch DONE")

        except Exception as e:
            self.logger.error(f"Bulk property prefetch failed: {e}")
            # On failure, caches remain empty - will fall back to individual calls
            self._property_groups = {}
            self._property_options = {}

    def _get_property_group_id(self, client, group_name: str) -> Optional[str]:
        """Get property group ID from local cache or fall back to API."""
        # Check local batch cache first
        if group_name in self._property_groups:
            return self._property_groups[group_name]

        # Fall back to API call (should rarely happen after bulk prefetch)
        from ecommerce_integrations.shopware6.export.property_handler import get_or_create_property_group
        group_id = get_or_create_property_group(client, group_name)
        if group_id:
            self._property_groups[group_name] = group_id
        return group_id

    def _get_property_option_id(self, client, group_id: str, group_name: str, option_value: str) -> Optional[str]:
        """Get property option ID from local cache or fall back to API."""
        key = f"{group_name}:{option_value}"

        # Check local batch cache first
        if key in self._property_options:
            return self._property_options[key]

        # Fall back to API call (should rarely happen after bulk prefetch)
        from ecommerce_integrations.shopware6.export.property_handler import get_or_create_variant_option
        option_id = get_or_create_variant_option(client, group_id, group_name, option_value)
        if option_id:
            self._property_options[key] = option_id
        return option_id

    def _batch_read_templates(self, item_codes: List[str]) -> Dict[str, Dict]:
        """Read multiple template items from ERPNext with their attributes."""
        if not item_codes:
            return {}

        # Get main item fields (including disabled - they sync as active=false)
        items = frappe.get_all(
            "Item",
            filters={"name": ["in", item_codes], "has_variants": 1},
            fields=[
                "name", "item_code", "item_name", "item_group",
                "description", "disabled", "has_variants",
                "weight_per_unit", "brand", "default_item_manufacturer",
                "ai_seo_title", "seo_title",
                "ai_seo_description", "seo_meta_description",
                "seo_keywords", "ai_long_description",
                # AI Benefits and Custom Fields
                "ai_benefits",
                "ai_short_description",
                "item_height", "item_width", "item_length",
                ITEM_SELLING_RATE_FIELD,
                "standard_rate",  # Fallback price for templates
                # Delivery time
                "delivery_time",
            ]
        )

        items_dict = {item.name: dict(item) for item in items}

        # Batch load attributes
        if items_dict:
            self._batch_load_template_attributes(list(items_dict.keys()), items_dict)

        # Batch fetch prices (for display price on parent)
        if items_dict:
            self._batch_load_prices(list(items_dict.keys()), items_dict)

        # Batch fetch taxes
        if items_dict:
            self._batch_load_taxes(list(items_dict.keys()), items_dict)

        # Batch fetch shopware_properties (custom fields from child table)
        if items_dict:
            self._batch_load_shopware_properties(list(items_dict.keys()), items_dict)

        return items_dict

    def _batch_load_template_attributes(self, item_codes: List[str], items_dict: Dict) -> None:
        """Load Item Variant Attributes for templates using ACTUAL variant values.

        IMPORTANT: This uses get_actual_variant_values() to get values that are
        actually used by variants, NOT the predefined values in Item Attribute Value.
        This ensures configuratorSettings match what variants actually have.
        """
        if not item_codes:
            return

        # Get all variant attributes for templates (from template's attribute definition)
        attributes = frappe.get_all(
            "Item Variant Attribute",
            filters={"parent": ["in", item_codes]},
            fields=["parent", "attribute"]
        )

        # Group attributes by template
        template_attrs = {}
        for attr in attributes:
            if attr.parent not in template_attrs:
                template_attrs[attr.parent] = set()
            template_attrs[attr.parent].add(attr.attribute)

        # For each template, get ACTUAL variant values (not predefined values!)
        for template_code, attr_names in template_attrs.items():
            if template_code not in items_dict:
                continue

            items_dict[template_code]["_attributes"] = []

            for attr_name in attr_names:
                # Use get_actual_variant_values to get values actually used by variants
                actual_values = get_actual_variant_values(template_code, attr_name)

                if actual_values:
                    items_dict[template_code]["_attributes"].append({
                        "name": attr_name,
                        "values": [{"value": v, "abbr": None} for v in actual_values]
                    })

    def _build_template_payload(
        self,
        client,
        item_data: Dict
    ) -> Tuple[Optional[Dict], bool, Optional[str]]:
        """Build Shopware template product payload with configuratorSettings.

        Returns:
            Tuple of (payload dict, is_new_product, skip_reason)
        """
        from ecommerce_integrations.shopware6.export.category_handler import sync_all_item_categories

        item_code = item_data["name"]

        # Check if exists
        existing_id = self._existing_products.get(item_code)
        is_new = not existing_id
        product_id = existing_id or generate_uuid(f"product_{item_code}")

        # Tax rate
        tax_rate = flt(item_data.get("_tax_rate") or 19.0)

        # Get tax ID
        if tax_rate not in self._tax_ids:
            self._tax_ids[tax_rate] = self._get_tax_id(client, tax_rate)
        tax_id = self._tax_ids.get(tax_rate)

        # Description
        description = (
            item_data.get("ai_long_description") or
            item_data.get("description") or
            item_data.get("item_name")
        )

        payload = {
            "id": product_id,
            "productNumber": item_code,
            "name": item_data.get("item_name") or item_code,
            "description": description,
            "active": not item_data.get("disabled"),
            "stock": 0,
            "isCloseout": False,
        }

        if tax_id:
            payload["taxId"] = tax_id

        # Price is required by Shopware, even for templates
        # Templates (parent products) don't have their own price, variants do
        # Use standard_rate if available, otherwise use 0 as placeholder
        gross_price = flt(item_data.get(ITEM_SELLING_RATE_FIELD)) or flt(item_data.get("standard_rate")) or 0
        net_price = gross_price / (1 + tax_rate / 100) if tax_rate and gross_price else gross_price
        payload["price"] = [{
            "currencyId": self._currency_id,
            "gross": gross_price,
            "net": net_price,
            "linked": False
        }]

        # Manufacturer
        manufacturer = item_data.get("brand") or item_data.get("default_item_manufacturer")
        if manufacturer:
            mfr_id = self._get_manufacturer_id(client, manufacturer)
            if mfr_id:
                payload["manufacturerId"] = mfr_id

        # SEO fields
        seo_title = item_data.get("ai_seo_title") or item_data.get("seo_title")
        seo_desc = item_data.get("ai_seo_description") or item_data.get("seo_meta_description")
        if seo_title:
            payload["metaTitle"] = seo_title
        if seo_desc:
            payload["metaDescription"] = seo_desc
        if item_data.get("seo_keywords"):
            payload["keywords"] = item_data["seo_keywords"]

        # Weight and dimensions
        if item_data.get("weight_per_unit"):
            payload["weight"] = flt(item_data["weight_per_unit"])
        if item_data.get("item_height"):
            payload["height"] = flt(item_data["item_height"]) * 10
        if item_data.get("item_width"):
            payload["width"] = flt(item_data["item_width"]) * 10
        if item_data.get("item_length"):
            payload["length"] = flt(item_data["item_length"]) * 10

        # Build configuratorSettings from attributes (with explicit IDs!)
        configurator_settings = []
        attributes = item_data.get("_attributes", [])
        seen_option_ids = set()

        for attr in attributes:
            attr_name = attr["name"]
            # Use local batch cache instead of individual API calls
            group_id = self._get_property_group_id(client, attr_name)

            if not group_id:
                continue

            for val in attr.get("values", []):
                attr_value = val["value"]
                # Use local batch cache instead of individual API calls
                option_id = self._get_property_option_id(client, group_id, attr_name, attr_value)

                if option_id and option_id not in seen_option_ids:
                    seen_option_ids.add(option_id)
                    # CRITICAL: Include explicit ID to avoid transaction rollback!
                    config_setting_id = generate_uuid(f"config_{product_id}_{option_id}")
                    configurator_settings.append({
                        "id": config_setting_id,
                        "optionId": option_id,
                    })

        if configurator_settings:
            payload["configuratorSettings"] = configurator_settings

            # Build variantListingConfig for expanded listings
            # displayParent=False: Don't show parent in listing, only variants
            # Limit expressionForListings to max 3 attributes to avoid
            # Shopware's O(n^k) display_group query explosion (consistent with template_handler.py)
            MAX_EXPRESSION_FOR_LISTINGS = 3
            unique_groups = []
            seen_group_ids = set()
            for attr in attributes:
                group_id = self._get_property_group_id(client, attr["name"])
                if group_id and group_id not in seen_group_ids:
                    seen_group_ids.add(group_id)
                    unique_groups.append(group_id)

            if unique_groups:
                payload["variantListingConfig"] = {
                    "displayParent": False,
                    "mainVariantId": None,
                    "configuratorGroupConfig": [
                        {
                            "id": gid,
                            "representation": "box",
                            "expressionForListings": idx < MAX_EXPRESSION_FOR_LISTINGS
                        }
                        for idx, gid in enumerate(unique_groups)
                    ]
                }

        # Categories (skip_sync=True because categories are synced in Phase 1)
        try:
            # sync_all_item_categories returns [{"id": cat_id}, ...]
            category_refs = sync_all_item_categories(client, item_code, skip_sync=True)
            if category_refs:
                payload["categories"] = category_refs
        except Exception:
            pass

        # Visibility - Multi-Channel support
        # ALWAYS handle visibilities separately after batch sync to avoid duplicate key errors
        # (Product might exist in Shopware even if not in ERPNext mappings)
        payload["_needs_visibility_update"] = True
        payload["_item_code"] = item_code

        # Custom Fields (AI Benefits, Short Description, Zubehoer, etc.)
        custom_fields = self._build_custom_fields(item_data)
        if custom_fields:
            payload["customFields"] = custom_fields

        # Delivery Time
        delivery_time_str = item_data.get("delivery_time")
        if delivery_time_str:
            delivery_time_id = self._get_delivery_time_id(client, delivery_time_str)
            if delivery_time_id:
                payload["deliveryTimeId"] = delivery_time_id

        return payload, is_new, None  # No skip reason - success

    # =========================================================================
    # BATCH VARIANT UPLOAD
    # =========================================================================

    def upload_variants(self, item_codes: List[str], skip_images: bool = False, start_batch: int = 1) -> BatchUploadResult:
        """
        Upload multiple variant items to Shopware in batches.
        Uses Sync API for bulk upsert with parentId and options.

        Args:
            item_codes: List of ERPNext variant item codes
            skip_images: Skip image sync for faster upload
            start_batch: Start from this batch number (1-based, for resuming interrupted syncs)

        Returns:
            BatchUploadResult with statistics
        """
        from ecommerce_integrations.shopware6.connection import get_shopware_client

        result = BatchUploadResult(total=len(item_codes))

        if not item_codes:
            return result

        client = get_shopware_client()
        if not client:
            self.logger.error("Could not get Shopware client")
            return result

        # Initialize caches
        self._init_caches(client)

        # Validate critical cache values - currency_id is required for price payloads
        if not self._currency_id:
            self.logger.error("Failed to get EUR currency ID from Shopware - cannot sync variants without valid currency")
            result.failed = len(item_codes)
            result.errors.append({
                "item_code": "INIT",
                "error": "Currency ID lookup failed - ensure EUR currency exists in Shopware"
            })
            return result

        # Load existing mappings for variants AND their parents
        all_codes = list(item_codes)

        # Get parent codes
        parent_codes = frappe.get_all(
            "Item",
            filters={"name": ["in", item_codes]},
            fields=["variant_of"],
            distinct=True,
            pluck="variant_of"
        )
        all_codes.extend([p for p in parent_codes if p])

        self._load_existing_mappings(all_codes)

        # Reconcile with Shopware - find products that exist in Shopware but not in ERPNext mappings
        self._reconcile_with_shopware(client, all_codes)

        # Process in batches
        total_batches = (len(item_codes) + BATCH_SIZE - 1) // BATCH_SIZE

        # Calculate starting position (start_batch is 1-based)
        start_index = (start_batch - 1) * BATCH_SIZE if start_batch > 1 else 0
        if start_batch > 1:
            self.logger.info(f"Resuming variants from batch {start_batch}/{total_batches} (skipping first {start_index} items)")

        for batch_start in range(start_index, len(item_codes), BATCH_SIZE):
            batch_codes = item_codes[batch_start:batch_start + BATCH_SIZE]
            batch_num = batch_start // BATCH_SIZE + 1

            try:
                batch_result = self._process_variant_batch(client, batch_codes, skip_images)

                result.success += batch_result.success
                result.failed += batch_result.failed
                result.skipped += batch_result.skipped
                result.errors.extend(batch_result.errors)
                result.created_ids.extend(batch_result.created_ids)
                result.updated_ids.extend(batch_result.updated_ids)
                result.processed_item_codes.extend(batch_result.processed_item_codes)
                result.skipped_items.extend(batch_result.skipped_items)

                frappe.db.commit()

                processed = min(batch_start + BATCH_SIZE, len(item_codes))
                self.logger.info(
                    f"Variant Batch {batch_num}/{total_batches}: "
                    f"{batch_result.success} success, {batch_result.failed} failed, {batch_result.skipped} skipped"
                )

                # Log to Ecommerce Integration Log (like category sync does)
                batch_summary = (
                    f"Variants Batch {batch_num}/{total_batches}: "
                    f"{batch_result.success} success, {batch_result.failed} failed, {batch_result.skipped} skipped"
                )
                create_shopware_log(
                    status="Success" if batch_result.failed == 0 else "Partial Success",
                    method="BatchProductUploader.upload_variants.batch",
                    message=batch_summary,
                    request_data={
                        "batch_num": batch_num,
                        "total_batches": total_batches,
                        "batch_size": len(batch_codes),
                        "success": batch_result.success,
                        "failed": batch_result.failed,
                        "skipped": batch_result.skipped,
                        "items": batch_result.processed_item_codes[:20],
                        "errors": batch_result.errors[:5] if batch_result.errors else [],
                        "skipped_items": batch_result.skipped_items[:10] if batch_result.skipped_items else []
                    }
                )

                if self.progress_callback:
                    try:
                        self.progress_callback(
                            batch_num, total_batches, processed, len(item_codes),
                            result.success, result.failed
                        )
                    except Exception as e:
                        self.logger.warning(f"Progress callback failed: {e}")

            except Exception as batch_error:
                # Log error but CONTINUE with next batch instead of aborting
                error_msg = str(batch_error)[:500]
                self.logger.error(
                    f"Variant Batch {batch_num}/{total_batches} FAILED with error: {error_msg}. "
                    f"Continuing with next batch..."
                )
                result.failed += len(batch_codes)
                for item_code in batch_codes:
                    result.errors.append({"item_code": item_code, "error": f"Batch failed: {error_msg[:100]}"})

                # Log batch failure
                create_shopware_log(
                    status="Error",
                    method="BatchProductUploader.upload_variants.batch",
                    message=f"Variants Batch {batch_num}/{total_batches} FAILED: {error_msg[:200]}",
                    request_data={
                        "batch_num": batch_num,
                        "total_batches": total_batches,
                        "batch_size": len(batch_codes),
                        "items": batch_codes[:20],
                        "error": error_msg
                    }
                )

                # Try to commit any partial work and reconnect if needed
                try:
                    frappe.db.commit()
                except Exception:
                    try:
                        frappe.db.rollback()
                        frappe.connect()
                    except Exception:
                        pass

        return result

    def _process_variant_batch(
        self,
        client,
        item_codes: List[str],
        skip_images: bool = False
    ) -> BatchUploadResult:
        """Process a batch of variant items using Shopware Sync API."""
        result = BatchUploadResult(total=len(item_codes))

        # Batch read variants from ERPNext
        variants_data = self._batch_read_variants(item_codes)

        if not variants_data:
            for ic in item_codes:
                result.skipped_items.append({"item_code": ic, "reason": "not_found_in_db"})
            result.skipped = len(item_codes)
            return result

        upsert_payloads = []
        create_ecom_items = []
        # Track variants needing visibility updates
        visibility_updates = []

        for item_code in item_codes:
            item_data = variants_data.get(item_code)
            if not item_data:
                result.skipped += 1
                result.skipped_items.append({"item_code": item_code, "reason": "not_found_in_batch_read"})
                continue

            try:
                payload, is_new, skip_reason = self._build_variant_payload(client, item_data)
                if payload:
                    # Check if this variant needs visibility update
                    if payload.pop("_needs_visibility_update", False):
                        variant_item_code = payload.pop("_item_code", item_code)
                        variant_of = payload.pop("_variant_of", None)
                        visibility_updates.append({
                            "product_id": payload["id"],
                            "item_code": variant_item_code,
                            "variant_of": variant_of,
                        })
                    else:
                        payload.pop("_item_code", None)
                        payload.pop("_variant_of", None)

                    upsert_payloads.append({
                        "payload": payload,
                        "item_code": item_code,
                        "is_new": is_new
                    })
                    if is_new:
                        create_ecom_items.append({
                            "item_code": item_code,
                            "shopware_id": payload["id"]
                        })
                else:
                    result.skipped += 1
                    result.skipped_items.append({"item_code": item_code, "reason": skip_reason or "unknown"})

            except Exception as e:
                result.failed += 1
                result.errors.append({
                    "item_code": item_code,
                    "error": str(e)[:200]
                })

        if not upsert_payloads:
            return result

        # Execute sync API call
        api_payloads = [p["payload"] for p in upsert_payloads]

        try:
            sync_payload = {
                "upsert-variants": {
                    "entity": "product",
                    "action": "upsert",
                    "payload": api_payloads
                }
            }

            _sync_with_retry(client, sync_payload, self.logger, f"variant upsert batch ({len(api_payloads)} items)")

            # Success
            result.success = len(upsert_payloads)
            result.processed_item_codes = [p["item_code"] for p in upsert_payloads]
            result.created_ids = [p["payload"]["id"] for p in upsert_payloads if p["is_new"]]
            result.updated_ids = [p["payload"]["id"] for p in upsert_payloads if not p["is_new"]]

            # Create Ecommerce Item records
            for ecom_data in create_ecom_items:
                try:
                    frappe.get_doc({
                        "doctype": "Ecommerce Item",
                        "integration": MODULE_NAME,
                        "erpnext_item_code": ecom_data["item_code"],
                        "integration_item_code": ecom_data["shopware_id"],
                        "has_variants": 0,
                        "sku": ecom_data["item_code"],
                    }).insert(ignore_permissions=True)
                except frappe.DuplicateEntryError:
                    pass

            # Update visibilities for variants (after successful batch sync)
            if visibility_updates:
                self._batch_update_visibilities(client, visibility_updates)

        except BaseException as e:
            result.failed = len(upsert_payloads)
            result.success = 0
            result.errors.append({
                "item_code": "batch_variant_sync",
                "error": str(e)[:500]
            })
            self.logger.error(f"Variant batch sync failed: {str(e)}")

        return result

    def _batch_read_variants(self, item_codes: List[str]) -> Dict[str, Dict]:
        """Read multiple variant items from ERPNext with their attribute values."""
        if not item_codes:
            return {}

        # Get main item fields (including disabled - they sync as active=false)
        items = frappe.get_all(
            "Item",
            filters={
                "name": ["in", item_codes],
                "variant_of": ["is", "set"]
            },
            fields=[
                "name", "item_code", "item_name", "item_group",
                "description", "disabled", "variant_of",
                "weight_per_unit", "brand", "default_item_manufacturer",
                # SEO fields
                "ai_seo_title", "seo_title",
                "ai_seo_description", "seo_meta_description",
                "seo_keywords",
                # Description fields
                "ai_long_description",
                # AI Benefits and Custom Fields
                "ai_benefits",
                "ai_short_description",
                # Dimensions
                "item_height", "item_width", "item_length",
                # Custom selling rate field
                ITEM_SELLING_RATE_FIELD,
                # Delivery time
                "delivery_time",
            ]
        )

        items_dict = {item.name: dict(item) for item in items}

        # Batch load variant attribute values
        if items_dict:
            self._batch_load_variant_attributes(list(items_dict.keys()), items_dict)

        # Batch fetch prices
        if items_dict:
            self._batch_load_prices(list(items_dict.keys()), items_dict)

        # Batch fetch barcodes
        if items_dict:
            self._batch_load_barcodes(list(items_dict.keys()), items_dict)

        # Batch fetch taxes
        if items_dict:
            self._batch_load_taxes(list(items_dict.keys()), items_dict)

        # Batch fetch shopware_properties (custom fields from child table)
        if items_dict:
            self._batch_load_shopware_properties(list(items_dict.keys()), items_dict)

        return items_dict

    def _batch_load_variant_attributes(self, item_codes: List[str], items_dict: Dict) -> None:
        """Load variant attribute values for variant items."""
        if not item_codes:
            return

        # Get all variant attributes
        attributes = frappe.get_all(
            "Item Variant Attribute",
            filters={"parent": ["in", item_codes]},
            fields=["parent", "attribute", "attribute_value"]
        )

        # Assign to items
        for attr in attributes:
            if attr.parent in items_dict:
                if "_variant_attrs" not in items_dict[attr.parent]:
                    items_dict[attr.parent]["_variant_attrs"] = {}
                items_dict[attr.parent]["_variant_attrs"][attr.attribute] = attr.attribute_value

    def _build_variant_payload(
        self,
        client,
        item_data: Dict
    ) -> Tuple[Optional[Dict], bool, Optional[str]]:
        """Build Shopware variant product payload with parentId and options.

        Returns:
            Tuple of (payload dict, is_new_product, skip_reason)
        """
        from ecommerce_integrations.shopware6.export.property_handler import (
            get_or_create_property_group,
            get_or_create_variant_option,
        )

        item_code = item_data["name"]
        parent_code = item_data.get("variant_of")

        if not parent_code:
            return None, False, "no_parent"

        # Get parent Shopware ID
        parent_id = self._existing_products.get(parent_code)
        if not parent_id:
            # Parent not synced - log warning
            self.logger.warning(f"Variant {item_code} skipped: Parent {parent_code} not found in Shopware")
            return None, False, f"parent_not_in_shopware:{parent_code}"

        # Check if variant exists
        existing_id = self._existing_products.get(item_code)
        is_new = not existing_id
        product_id = existing_id or generate_uuid(f"product_{item_code}")

        # Get price
        price = flt(item_data.get("_price") or item_data.get(ITEM_SELLING_RATE_FIELD) or 0)
        if price <= 0:
            return None, False, "no_price"

        # Tax rate
        tax_rate = flt(item_data.get("_tax_rate") or 19.0)
        gross_price = round(price * (1 + tax_rate / 100), 2)

        # Get tax ID
        if tax_rate not in self._tax_ids:
            self._tax_ids[tax_rate] = self._get_tax_id(client, tax_rate)
        tax_id = self._tax_ids.get(tax_rate)

        # Description
        description = (
            item_data.get("ai_long_description") or
            item_data.get("description") or
            item_data.get("item_name")
        )

        payload = {
            "id": product_id,
            "parentId": parent_id,
            "productNumber": item_code,
            "name": item_data.get("item_name") or item_code,
            "description": description,
            "active": not item_data.get("disabled"),
            "stock": 0,
            "isCloseout": False,
            "price": [{
                "currencyId": self._currency_id,
                "gross": gross_price,
                "net": price,
                "linked": False,
            }],
        }

        if tax_id:
            payload["taxId"] = tax_id

        # Barcode/EAN
        if item_data.get("_barcode"):
            payload["ean"] = item_data["_barcode"]

        # Weight and dimensions
        if item_data.get("weight_per_unit"):
            payload["weight"] = flt(item_data["weight_per_unit"])
        if item_data.get("item_height"):
            payload["height"] = flt(item_data["item_height"]) * 10
        if item_data.get("item_width"):
            payload["width"] = flt(item_data["item_width"]) * 10
        if item_data.get("item_length"):
            payload["length"] = flt(item_data["item_length"]) * 10

        # SEO fields
        seo_title = item_data.get("ai_seo_title") or item_data.get("seo_title")
        seo_desc = item_data.get("ai_seo_description") or item_data.get("seo_meta_description")

        if seo_title:
            payload["metaTitle"] = seo_title
        if seo_desc:
            payload["metaDescription"] = seo_desc
        if item_data.get("seo_keywords"):
            payload["keywords"] = item_data["seo_keywords"]

        # Build options from variant attributes
        options = []
        variant_attrs = item_data.get("_variant_attrs", {})

        for attr_name, attr_value in variant_attrs.items():
            if not attr_value:
                continue
            group_id = get_or_create_property_group(client, attr_name)
            if group_id:
                option_id = get_or_create_variant_option(client, group_id, attr_name, attr_value)
                if option_id:
                    options.append({"id": option_id})

        if options:
            payload["options"] = options

        # Properties from shopware_properties table (type "Property" becomes filter properties)
        from ecommerce_integrations.shopware6.export.property_handler import (
            get_or_create_property_option,
        )

        shopware_properties = item_data.get("_shopware_properties") or []
        property_ids = []
        for prop in shopware_properties:
            # "Property" and "Text" types go to Shopware properties (Technische Daten)
            # "Text" is treated the same as "Property" for backwards compatibility
            prop_type = prop.get("property_type")
            if prop_type in ("Property", "Text") and prop.get("property_value"):
                group_id = get_or_create_property_group(client, prop["property_name"])
                if group_id:
                    option_id = get_or_create_property_option(
                        client, group_id, prop["property_name"], prop["property_value"]
                    )
                    if option_id:
                        property_ids.append({"id": option_id})

        if property_ids:
            payload["properties"] = property_ids

        # Custom Fields (AI Benefits, Short Description, Zubehoer, etc.)
        custom_fields = self._build_custom_fields(item_data)
        if custom_fields:
            payload["customFields"] = custom_fields

        # Delivery Time
        delivery_time_str = item_data.get("delivery_time")
        if delivery_time_str:
            delivery_time_id = self._get_delivery_time_id(client, delivery_time_str)
            if delivery_time_id:
                payload["deliveryTimeId"] = delivery_time_id

        # Visibility - Multi-Channel support for variants
        # Variants need their own visibilities (they don't inherit from parent in Shopware)
        # Use parent item for visibility calculation (consistent with variant_handler.py)
        payload["_needs_visibility_update"] = True
        payload["_item_code"] = item_code
        payload["_variant_of"] = parent_code  # Signal to use parent for visibility

        return payload, is_new, None  # No skip reason - success


def batch_upload_products(item_codes: List[str], skip_images: bool = True) -> Dict[str, Any]:
    """
    Convenience function for batch uploading products.

    Args:
        item_codes: List of ERPNext item codes
        skip_images: Skip image sync (default True for speed)

    Returns:
        Dict with upload statistics
    """
    uploader = BatchProductUploader()
    result = uploader.upload_items(item_codes, skip_images=skip_images)

    return {
        "total": result.total,
        "success": result.success,
        "failed": result.failed,
        "skipped": result.skipped,
        "errors": result.errors[:20],  # Limit errors in response
        "created": len(result.created_ids),
        "updated": len(result.updated_ids),
        "processed_item_codes": result.processed_item_codes,
    }


def get_all_item_codes_by_type() -> Dict[str, List[str]]:
    """Get all item codes grouped by type (including disabled - they sync as active=false)."""

    # Templates (has_variants=1) - including disabled (will be synced as active=false)
    templates = frappe.get_all(
        "Item",
        filters={"has_variants": 1},
        pluck="name"
    )

    # Simple items (no variants, not a variant) - including disabled
    simple_items = frappe.get_all(
        "Item",
        filters={
            "has_variants": 0,
            "variant_of": ["is", "not set"]
        },
        pluck="name"
    )

    # Variants (variant_of is set) - including disabled
    variants = frappe.get_all(
        "Item",
        filters={
            "variant_of": ["is", "set"]
        },
        pluck="name"
    )

    return {
        "templates": templates,
        "simple_items": simple_items,
        "variants": variants
    }
