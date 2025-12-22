# Copyright (c) 2024, Frappe and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class RAGSetting(Document):
    def validate(self):
        if self.enabled:
            # Pinecone validation
            if not self.pinecone_api_key:
                frappe.throw("Pinecone API Key is required")
            if not self.pinecone_index_name:
                frappe.throw("Pinecone Index Name is required")

            # Embedding provider validation
            if self.embedding_provider == "Google":
                if not self.google_api_key:
                    frappe.throw("Google API Key is required for Google embeddings")
            elif self.embedding_provider == "OpenAI":
                if not self.openai_api_key:
                    frappe.throw("OpenAI API Key is required for OpenAI embeddings")

        # Auto-set dimensions based on model
        self._set_dimensions()

    def _set_dimensions(self):
        """Set dimensions based on selected embedding model"""
        if self.embedding_provider == "Google":
            model = self.embedding_model or "text-embedding-004"
            if model == "gemini-embedding-001":
                self.dimensions = 3072  # gemini-embedding-001 (latest Google model)
            else:
                self.dimensions = 768  # text-embedding-004
        elif self.embedding_provider == "OpenAI":
            model = self.embedding_model or "text-embedding-3-small"
            if model == "text-embedding-3-large":
                self.dimensions = 3072
            else:
                self.dimensions = 1536
        else:
            # Local sentence-transformers
            self.dimensions = 768

    @frappe.whitelist()
    def test_connection(self):
        from ecommerce_integrations.rag.connection import test_connection
        return test_connection()

    @frappe.whitelist()
    def create_index(self):
        """Create Pinecone index if it doesn't exist"""
        from ecommerce_integrations.rag.connection import create_index_if_not_exists
        return create_index_if_not_exists()

    @frappe.whitelist()
    def start_full_sync(self):
        from ecommerce_integrations.rag.product_export import full_sync
        return full_sync()

    @frappe.whitelist()
    def get_queue_status(self):
        from ecommerce_integrations.rag.bulk_sync import get_queue_status
        return get_queue_status()

    @frappe.whitelist()
    def force_process_queue(self):
        from ecommerce_integrations.rag.bulk_sync import force_process_queue
        return force_process_queue()
