# Copyright (c) 2024, Frappe and contributors
# For license information, please see license.txt

# Fields that trigger a re-sync when updated
ITEM_SYNC_FIELDS = [
    "item_name",
    "description",
    "item_group",
    "brand",
    "is_sales_item",
    "disabled"
]

# Embedding models and their dimensions
EMBEDDING_MODELS = {
    "text-embedding-005": 768,
    "text-multilingual-embedding-002": 768,
    "text-embedding-004": 768,
}

# Default batch sizes
DEFAULT_BATCH_SIZE = 50
DEFAULT_EMBEDDING_BATCH_SIZE = 250

# Redis key prefix
REDIS_PREFIX = "rag:"
