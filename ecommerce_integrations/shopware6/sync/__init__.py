"""
Shopware 6 Sync Module

Unified synchronization layer for ERPNext <-> Shopware data.

Core principle: ERPNext is ALWAYS the source of truth.

Modules:
- conflict_detector: Detects differences between ERPNext and Shopware
- sync_manager: Unified entry point for all sync operations

Usage:
    from ecommerce_integrations.shopware6.sync import SyncManager

    manager = SyncManager()

    # Full reconciliation - makes Shopware IDENTICAL to ERPNext
    result = manager.full_reconciliation(
        sync_categories=True,
        sync_products=True,
        sync_images=True,
        cleanup_orphans=True
    )
"""

from ecommerce_integrations.shopware6.sync.conflict_detector import (
    ConflictDetector,
    ConflictReport,
    ConflictStrategy,
    FieldDifference,
    detect_all_conflicts,
)
from ecommerce_integrations.shopware6.sync.sync_manager import (
    SyncManager,
    enqueue_full_reconciliation_no_brainer,
    full_reconciliation_no_brainer,
    quick_reconciliation,
)

__all__ = [
    # Conflict detection
    "ConflictDetector",
    "ConflictReport",
    "ConflictStrategy",
    "FieldDifference",
    # Sync manager
    "SyncManager",
    "detect_all_conflicts",
    "enqueue_full_reconciliation_no_brainer",
    "full_reconciliation_no_brainer",
    "quick_reconciliation",
]
