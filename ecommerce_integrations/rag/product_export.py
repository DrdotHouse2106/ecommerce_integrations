# Copyright (c) 2024, Frappe and contributors
# For license information, please see license.txt

import frappe
from frappe.utils import now_datetime
from .connection import get_pinecone_index, get_rag_settings
from .embedding import generate_embedding, generate_embeddings_batch, build_product_text
from ecommerce_integrations.rag.services import create_rag_log, require_rag_admin


def upload_item_to_rag(item_code: str):
    """Upload single item to Pinecone"""
    settings = get_rag_settings()

    if not settings.enabled:
        return

    try:
        item = frappe.get_doc("Item", item_code)

        # Build text for embedding
        text = build_product_text(item)

        if not text or len(text.strip()) < 10:
            create_rag_log(
                status="Skipped",
                method="upload_item_to_rag",
                item_code=item_code,
                message="No text for embedding"
            )
            return

        # Generate embedding
        embedding = generate_embedding(text)

        if not embedding:
            create_rag_log(
                status="Error",
                method="upload_item_to_rag",
                item_code=item_code,
                message="Failed to generate embedding"
            )
            return

        # Get Pinecone index
        index = get_pinecone_index()

        # Build metadata for filtering (with category hierarchy)
        metadata = build_item_metadata(item, text)

        # Upsert to Pinecone
        index.upsert(
            vectors=[{
                "id": item_code,
                "values": embedding,
                "metadata": metadata
            }]
        )

        create_rag_log(
            status="Success",
            method="upload_item_to_rag",
            item_code=item_code,
            request_data={"text": text[:500], "embedding_length": len(embedding)},
            message="Item synced successfully"
        )

    except Exception as e:
        create_rag_log(
            status="Error",
            method="upload_item_to_rag",
            item_code=item_code,
            exception=str(e)
        )
        frappe.log_error(message=str(e), title=f"RAG Sync Error: {item_code}")


def upload_items_batch_to_rag(item_codes: list):
    """Upload multiple items in batch to Pinecone"""
    settings = get_rag_settings()

    if not settings.enabled:
        return

    try:
        # Load items
        items = []
        texts = []

        for item_code in item_codes:
            try:
                item = frappe.get_doc("Item", item_code)
                text = build_product_text(item)
                if text and len(text.strip()) >= 10:
                    items.append(item)
                    texts.append(text)
            except frappe.DoesNotExistError:
                continue

        if not texts:
            return

        # Generate batch embeddings
        embeddings = generate_embeddings_batch(texts)

        if len(embeddings) != len(items):
            frappe.log_error(
                message=f"Embedding count mismatch: {len(embeddings)} vs {len(items)}",
                title="RAG Batch Sync Warning"
            )
            return

        # Get Pinecone index
        index = get_pinecone_index()

        # Create vectors for Pinecone
        vectors = []
        for item, embedding, text in zip(items, embeddings, texts):
            vectors.append({
                "id": item.item_code,
                "values": embedding,
                "metadata": build_item_metadata(item, text)
            })

        # Batch upsert to Pinecone (max 100 per request)
        BATCH_SIZE = 100
        for i in range(0, len(vectors), BATCH_SIZE):
            batch = vectors[i:i + BATCH_SIZE]
            index.upsert(vectors=batch)

        create_rag_log(
            status="Success",
            method="upload_items_batch_to_rag",
            message=f"{len(vectors)} items synced"
        )

    except Exception as e:
        create_rag_log(
            status="Error",
            method="upload_items_batch_to_rag",
            exception=str(e)
        )
        frappe.log_error(message=str(e), title="RAG Batch Sync Error")


def delete_item_from_rag(doc, method=None):
    """Delete item from Pinecone (hook for on_trash)"""
    settings = get_rag_settings()

    if not settings.enabled:
        return

    item_code = doc.item_code if hasattr(doc, 'item_code') else doc.name

    try:
        index = get_pinecone_index()
        index.delete(ids=[item_code])

        create_rag_log(
            status="Success",
            method="delete_item_from_rag",
            item_code=item_code,
            message="Item deleted from RAG"
        )

    except Exception as e:
        frappe.log_error(message=str(e), title=f"RAG Delete Error: {item_code}")


@frappe.whitelist()
def full_sync():
    """Full sync of all items"""
    require_rag_admin()
    frappe.enqueue(
        _full_sync_job,
        queue="long",
        timeout=7200  # 2 hours
    )
    return {"status": "Full sync started"}


def _full_sync_job():
    """Background job for full sync"""
    settings = get_rag_settings()

    if not settings.enabled:
        return

    # Build filters
    filters = {"disabled": 0}

    if settings.sync_only_sellable:
        filters["is_sales_item"] = 1

    if settings.item_group_filter:
        # Get all child groups
        groups = get_child_item_groups(settings.item_group_filter)
        filters["item_group"] = ["in", groups]

    # Get all items
    item_codes = frappe.get_all("Item", filters=filters, pluck="name")

    if not item_codes:
        frappe.logger().info("RAG Full Sync: No items to sync")
        return

    # Process in batches
    batch_size = min(settings.bulk_sync_batch_size or 100, 100)  # Pinecone max 100
    total = len(item_codes)
    synced = 0
    errors = 0

    frappe.logger().info(f"RAG Full Sync: Starting sync of {total} items")

    for i in range(0, total, batch_size):
        batch = item_codes[i:i + batch_size]

        try:
            upload_items_batch_to_rag(batch)
            synced += len(batch)
            frappe.db.commit()

            # Log progress
            progress = (synced / total) * 100
            frappe.logger().info(f"RAG Full Sync: {synced}/{total} ({progress:.1f}%)")

            # Publish realtime progress
            frappe.publish_progress(
                percent=progress,
                title="RAG Full Sync",
                description=f"{synced}/{total} Items"
            )

        except Exception as e:
            errors += 1
            frappe.log_error(message=str(e), title=f"RAG Full Sync Error - Batch {i}")

        # Rate limiting
        import time
        time.sleep(0.5)

    # Update stats
    settings.reload()
    settings.last_sync = now_datetime()
    settings.items_synced = synced
    settings.save(ignore_permissions=True)
    frappe.db.commit()

    create_rag_log(
        status="Success" if errors == 0 else "Error",
        method="full_sync",
        message=f"Full sync completed: {synced}/{total} items synced, {errors} errors"
    )

    frappe.logger().info(f"RAG Full Sync: Completed - {synced}/{total} items, {errors} errors")


def get_child_item_groups(parent_group):
    """Get all child groups of an Item Group"""
    groups = [parent_group]

    children = frappe.get_all(
        "Item Group",
        filters={"parent_item_group": parent_group},
        pluck="name"
    )

    for child in children:
        groups.extend(get_child_item_groups(child))

    return groups


def get_item_group_hierarchy(item_group):
    """Get the full hierarchy of an item group (from leaf to root)

    Returns dict with:
    - item_group: the direct item group
    - parent_group: immediate parent
    - root_group: top-level category (e.g., Transportgeräte, Regalsysteme)
    - hierarchy: list of all groups from leaf to root
    """
    if not item_group:
        return {
            "item_group": "",
            "parent_group": "",
            "root_group": "",
            "hierarchy": []
        }

    hierarchy = [item_group]
    current = item_group

    # Walk up the tree
    max_depth = 10  # Prevent infinite loops
    while max_depth > 0:
        parent = frappe.db.get_value("Item Group", current, "parent_item_group")
        if not parent or parent == "All Item Groups":
            break
        hierarchy.append(parent)
        current = parent
        max_depth -= 1

    # Top-level categories we care about for filtering
    TOP_LEVEL_CATEGORIES = [
        "Transportgeräte",
        "Regalsysteme",
        "Betriebseinrichtungen",
        "Lagerzubehör",
        "Service",
        "Themenwelten",
        "Sicherheit und Umwelt"
    ]

    # Find the root category (highest level that's in our list)
    root_group = ""
    for group in hierarchy:
        if group in TOP_LEVEL_CATEGORIES:
            root_group = group
            break

    return {
        "item_group": item_group,
        "parent_group": hierarchy[1] if len(hierarchy) > 1 else "",
        "root_group": root_group,
        "hierarchy": hierarchy
    }


def get_item_price(item_code):
    """Get the selling price for an item from Item Price or standard_rate"""
    # Try to get price from Item Price (Standard Selling price list)
    price = frappe.db.get_value(
        "Item Price",
        {
            "item_code": item_code,
            "selling": 1,
            "price_list": ["like", "%Selling%"]
        },
        "price_list_rate"
    )

    if price:
        return float(price)

    # Fallback to standard_rate on Item
    standard_rate = frappe.db.get_value("Item", item_code, "standard_rate")
    if standard_rate:
        return float(standard_rate)

    return None


def build_item_metadata(item, text):
    """Build metadata dict for Pinecone with category hierarchy and custom fields"""
    hierarchy = get_item_group_hierarchy(item.item_group)
    settings = get_rag_settings()

    # Get price
    price = get_item_price(item.item_code)

    metadata = {
        "item_code": item.item_code,
        "item_name": item.item_name or "",
        "item_group": item.item_group or "",
        "parent_group": hierarchy["parent_group"],
        "root_group": hierarchy["root_group"],  # For filtering by main category!
        "brand": item.brand or "",
        "price": price if price else 0.0,  # Include price for RAG filtering/display
        "text": text[:1000]
    }

    # Add custom fields as separate metadata for filtering
    if settings.include_custom_fields:
        custom_fields = [f.strip() for f in settings.include_custom_fields.split(",") if f.strip()]
        for field in custom_fields:
            value = item.get(field) if hasattr(item, 'get') else getattr(item, field, None)
            if value is not None and value != "":
                # Convert to string and clean up
                if not isinstance(value, str):
                    value = str(value)
                # Strip HTML if present
                if "<" in value:
                    value = frappe.utils.strip_html(value)
                value = value.strip()
                if value:
                    # Use clean field name as key (e.g., "jattr_farbe" -> "farbe")
                    clean_key = field.replace("jattr_", "").lower()
                    metadata[clean_key] = value

    return metadata


@frappe.whitelist()
def sync_single_item(item_code):
    """Manually sync a single item"""
    require_rag_admin()
    frappe.enqueue(
        upload_item_to_rag,
        item_code=item_code,
        queue="short"
    )
    return {"status": f"Sync started for {item_code}"}


@frappe.whitelist()
def search_products(query: str, top_k: int = 10, filters: dict = None):
    """Search products using semantic similarity"""
    require_rag_admin()
    settings = get_rag_settings()
    top_k = max(1, min(int(top_k), 50))

    if not settings.enabled:
        return {"error": "RAG is not enabled"}

    try:
        # Generate embedding for query
        query_embedding = generate_embedding(query)

        if not query_embedding:
            return {"error": "Failed to generate query embedding"}

        # Get Pinecone index
        index = get_pinecone_index()

        # Build filter if provided (supports any metadata field including custom fields)
        pinecone_filter = None
        if filters:
            pinecone_filter = {}
            for key, value in filters.items():
                if value:  # Skip empty values
                    pinecone_filter[key] = {"$eq": value}

        # Search
        results = index.query(
            vector=query_embedding,
            top_k=top_k,
            include_metadata=True,
            filter=pinecone_filter
        )

        # Format results
        products = []
        for match in results.matches:
            products.append({
                "item_code": match.id,
                "score": match.score,
                "item_name": match.metadata.get("item_name", ""),
                "item_group": match.metadata.get("item_group", ""),
                "brand": match.metadata.get("brand", ""),
                "text_preview": match.metadata.get("text", "")[:200]
            })

        return {
            "query": query,
            "results": products,
            "count": len(products)
        }

    except Exception as e:
        frappe.log_error(message=str(e), title="RAG Search Error")
        return {"error": str(e)}
