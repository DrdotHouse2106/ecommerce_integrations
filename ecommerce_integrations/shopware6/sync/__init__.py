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
    ConflictStrategy,
    ConflictReport,
    FieldDifference,
    detect_all_conflicts,
)

from ecommerce_integrations.shopware6.sync.sync_manager import (
    SyncManager,
    quick_reconciliation,
    full_reconciliation_no_brainer,
    enqueue_full_reconciliation_no_brainer,
)

__all__ = [
    # Conflict detection
    "ConflictDetector",
    "ConflictStrategy",
    "ConflictReport",
    "FieldDifference",
    "detect_all_conflicts",
    # Sync manager
    "SyncManager",
    "quick_reconciliation",
    "full_reconciliation_no_brainer",
    "enqueue_full_reconciliation_no_brainer",
]
