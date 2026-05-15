# Copyright (c) 2024, Frappe and contributors
# For license information, please see license.txt

import frappe
from frappe import _


def get_rag_settings():
    """Get RAG settings singleton"""
    return frappe.get_single("RAG Setting")


def get_pinecone_client():
    """Get Pinecone client instance"""
    from pinecone import Pinecone
    settings = get_rag_settings()

    if not settings.pinecone_api_key:
        frappe.throw(_("Pinecone API Key not configured"))

    return Pinecone(api_key=settings.get_password("pinecone_api_key"))


def get_pinecone_index():
    """Get Pinecone index for operations"""
    settings = get_rag_settings()
    pc = get_pinecone_client()

    index_name = settings.pinecone_index_name

    # Check if index exists
    existing_indexes = [idx.name for idx in pc.list_indexes()]

    if index_name not in existing_indexes:
        frappe.throw(_("Pinecone index '{0}' does not exist. Please create it first.").format(index_name))

    # Get the index
    if settings.pinecone_environment:
        # Pod-based index
        return pc.Index(index_name, host=settings.pinecone_environment)
    else:
        # Serverless index (auto-detect host)
        return pc.Index(index_name)


def create_index_if_not_exists():
    """Create Pinecone index if it doesn't exist"""
    from pinecone import ServerlessSpec
    settings = get_rag_settings()
    pc = get_pinecone_client()

    index_name = settings.pinecone_index_name
    existing_indexes = [idx.name for idx in pc.list_indexes()]

    if index_name in existing_indexes:
        return {"status": "exists", "index_name": index_name}

    # Get dimensions based on embedding model
    dimensions = get_embedding_dimensions()

    # Create serverless index (free tier)
    pc.create_index(
        name=index_name,
        dimension=dimensions,
        metric="cosine",
        spec=ServerlessSpec(
            cloud="aws",
            region="us-east-1"  # Free tier region
        )
    )

    return {"status": "created", "index_name": index_name, "dimensions": dimensions}


def get_embedding_dimensions():
    """Get embedding dimensions based on model"""
    settings = get_rag_settings()

    if settings.embedding_provider == "OpenAI":
        model = settings.embedding_model or "text-embedding-3-small"
        dimensions_map = {
            "text-embedding-3-small": 1536,
            "text-embedding-3-large": 3072,
            "text-embedding-ada-002": 1536
        }
        return dimensions_map.get(model, 1536)
    else:
        # Local sentence-transformers (paraphrase-multilingual-mpnet-base-v2)
        return 768


def test_connection():
    """Test connection to Pinecone"""
    try:
        settings = get_rag_settings()

        if not settings.enabled:
            return {
                "success": False,
                "error": "RAG Sync is not enabled"
            }

        pc = get_pinecone_client()

        # List indexes to verify connection
        indexes = pc.list_indexes()
        index_names = [idx.name for idx in indexes]

        # Check if our index exists
        index_name = settings.pinecone_index_name
        index_exists = index_name in index_names

        result = {
            "success": True,
            "provider": "Pinecone",
            "index_name": index_name,
            "index_exists": index_exists,
            "available_indexes": index_names
        }

        # Get index stats if exists
        if index_exists:
            index = get_pinecone_index()
            stats = index.describe_index_stats()
            result["vector_count"] = stats.total_vector_count
            result["dimensions"] = stats.dimension

        return result

    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


def delete_index():
    """Delete the Pinecone index (use with caution!)"""
    settings = get_rag_settings()
    pc = get_pinecone_client()

    index_name = settings.pinecone_index_name
    pc.delete_index(index_name)

    return {"status": "deleted", "index_name": index_name}
