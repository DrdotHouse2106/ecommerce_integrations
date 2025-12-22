# Copyright (c) 2024, Frappe and contributors
# For license information, please see license.txt

import frappe
from .connection import get_rag_settings


def get_openai_client():
    """Get OpenAI client for embeddings"""
    from openai import OpenAI

    settings = get_rag_settings()

    if not settings.openai_api_key:
        frappe.throw("OpenAI API Key not configured")

    return OpenAI(api_key=settings.get_password("openai_api_key"))


def get_google_client():
    """Get Google Generative AI client for embeddings"""
    import google.generativeai as genai

    settings = get_rag_settings()

    if not settings.google_api_key:
        frappe.throw("Google API Key not configured")

    genai.configure(api_key=settings.get_password("google_api_key"))
    return genai


def generate_embedding(text: str) -> list:
    """Generate embedding for a single text"""
    if not text or not text.strip():
        return None

    settings = get_rag_settings()

    if settings.embedding_provider == "Google":
        return _generate_google_embedding(text)
    elif settings.embedding_provider == "OpenAI":
        return _generate_openai_embedding(text)
    else:
        return _generate_local_embedding(text)


def _generate_google_embedding(text: str) -> list:
    """Generate embedding using Google Gemini API (FREE!)"""
    from .connection import get_rag_settings

    genai = get_google_client()
    settings = get_rag_settings()

    # Determine model based on settings
    model_name = "models/text-embedding-004"  # default
    if settings.embedding_model == "gemini-embedding-001":
        model_name = "models/gemini-embedding-001"

    result = genai.embed_content(
        model=model_name,
        content=text,
        task_type="retrieval_document"
    )

    return result['embedding']


def _generate_openai_embedding(text: str) -> list:
    """Generate embedding using OpenAI API"""
    settings = get_rag_settings()
    client = get_openai_client()

    model = settings.embedding_model or "text-embedding-3-small"

    response = client.embeddings.create(
        model=model,
        input=text
    )

    return response.data[0].embedding


def _generate_local_embedding(text: str) -> list:
    """Generate embedding using local sentence-transformers"""
    try:
        from sentence_transformers import SentenceTransformer

        # Use a good multilingual model
        model = SentenceTransformer('paraphrase-multilingual-mpnet-base-v2')
        embedding = model.encode(text)

        return embedding.tolist()

    except ImportError:
        frappe.throw(
            "sentence-transformers not installed. "
            "Install with: pip install sentence-transformers"
        )


def generate_embeddings_batch(texts: list) -> list:
    """Generate embeddings for multiple texts"""
    if not texts:
        return []

    settings = get_rag_settings()

    if settings.embedding_provider == "Google":
        return _generate_google_embeddings_batch(texts)
    elif settings.embedding_provider == "OpenAI":
        return _generate_openai_embeddings_batch(texts)
    else:
        return _generate_local_embeddings_batch(texts)


def _generate_google_embeddings_batch(texts: list) -> list:
    """Generate embeddings using Google Gemini API (batch) - FREE!"""
    from .connection import get_rag_settings

    genai = get_google_client()
    settings = get_rag_settings()

    # Determine model based on settings
    model_name = "models/text-embedding-004"  # default
    if settings.embedding_model == "gemini-embedding-001":
        model_name = "models/gemini-embedding-001"

    # Replace empty texts with placeholder
    texts = [t if t and t.strip() else "empty" for t in texts]

    # Google allows batch embedding
    # Process in batches of 100 to be safe
    BATCH_SIZE = 100
    all_embeddings = []

    for i in range(0, len(texts), BATCH_SIZE):
        batch = texts[i:i + BATCH_SIZE]

        result = genai.embed_content(
            model=model_name,
            content=batch,
            task_type="retrieval_document"
        )

        all_embeddings.extend(result['embedding'])

    return all_embeddings


def _generate_openai_embeddings_batch(texts: list) -> list:
    """Generate embeddings using OpenAI API (batch)"""
    settings = get_rag_settings()
    client = get_openai_client()

    model = settings.embedding_model or "text-embedding-3-small"

    # OpenAI allows up to 2048 inputs per request
    BATCH_SIZE = 2000
    all_embeddings = []

    for i in range(0, len(texts), BATCH_SIZE):
        batch = texts[i:i + BATCH_SIZE]

        # Replace empty texts with placeholder
        batch = [t if t and t.strip() else "empty" for t in batch]

        response = client.embeddings.create(
            model=model,
            input=batch
        )

        # Sort by index to ensure correct order
        sorted_data = sorted(response.data, key=lambda x: x.index)
        all_embeddings.extend([item.embedding for item in sorted_data])

    return all_embeddings


def _generate_local_embeddings_batch(texts: list) -> list:
    """Generate embeddings using local sentence-transformers (batch)"""
    try:
        from sentence_transformers import SentenceTransformer

        model = SentenceTransformer('paraphrase-multilingual-mpnet-base-v2')

        # Replace empty texts
        texts = [t if t and t.strip() else "empty" for t in texts]

        embeddings = model.encode(texts)

        return [emb.tolist() for emb in embeddings]

    except ImportError:
        frappe.throw(
            "sentence-transformers not installed. "
            "Install with: pip install sentence-transformers"
        )


def build_product_text(item_doc) -> str:
    """Build the text for embedding from Item fields"""
    settings = get_rag_settings()
    parts = []

    if settings.include_item_name and item_doc.item_name:
        parts.append(f"Produkt: {item_doc.item_name}")

    if settings.include_description and item_doc.description:
        # Remove HTML tags
        description = frappe.utils.strip_html(item_doc.description)
        if description:
            parts.append(f"Beschreibung: {description[:2000]}")  # Limit description length

    if settings.include_item_group and item_doc.item_group:
        parts.append(f"Kategorie: {item_doc.item_group}")

    if settings.include_brand and item_doc.brand:
        parts.append(f"Marke: {item_doc.brand}")

    # Custom Fields
    if settings.include_custom_fields:
        custom_fields = [f.strip() for f in settings.include_custom_fields.split(",") if f.strip()]
        for field in custom_fields:
            # Use doc.get() for Frappe documents (more reliable for custom fields)
            value = item_doc.get(field) if hasattr(item_doc, 'get') else getattr(item_doc, field, None)
            if value is not None and value != "":
                # Convert non-string values to string
                if not isinstance(value, str):
                    value = str(value)
                # Strip HTML if it looks like HTML
                if "<" in value:
                    value = frappe.utils.strip_html(value)
                # Skip if value is empty after stripping
                if not value.strip():
                    continue
                # Create readable field name
                field_label = field.replace("jattr_", "").replace("_", " ").title()
                parts.append(f"{field_label}: {value}")

    # Item Code for reference
    parts.append(f"Artikelnummer: {item_doc.item_code}")

    return "\n".join(parts)
