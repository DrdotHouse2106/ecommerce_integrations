"""Dataclasses describing the dry-run preview of a Product Sync run.

A :class:`ProductSyncPreviewPlan` describes — without side effects —
what :func:`tasks.apply_sync` *would* do if invoked live. The plan is
serialisable JSON, returned by the form's preview endpoint and rendered
by the JS dialog.

Shape mirrors :mod:`catalog_mirror.engine.preview` (same ``to_dict``,
same dataclass split, same ``notes`` escape hatch) so the JS overlay
can reuse most of the existing rendering code. Where the Mirror has
moves we have deactivates; otherwise the buckets line up.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class FieldDiff:
    """One drift field for an update plan.

    ``current`` is the value the backend reports today; ``proposed`` is
    what the differ would push. Both are stringified for display — the
    form renders a side-by-side cell, not a typed comparison.

    ``change_kind`` lets the renderer pick a pill color without
    re-deriving it from the values (which is fiddly for long-text /
    list fields). ``risk_flag`` flags fields that hit a heuristic
    threshold (``price_jump`` for >20% price drift, ``stock_drop`` for
    >50% stock decrease, ``name_rewrite`` for fully replaced names) so
    the risk banner can count them without re-scanning.

    ``preview_current`` / ``preview_proposed`` are short (≤200 chars)
    previews of long-text fields used in the inline table cell; the
    full values stay in ``current`` / ``proposed`` for the modal
    expand view.
    """

    field: str
    current: str | None
    proposed: str | None
    change_kind: str = "modified"  # added | removed | modified | unchanged
    risk_flag: str | None = None   # price_jump | stock_drop | name_rewrite | None
    preview_current: str | None = None
    preview_proposed: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ProductNodePlan:
    """What would happen to one Item during the sync.

    The dataclass is intentionally narrow — only the fields the JS
    dialog needs to render a row. ``diffs`` is populated for updates;
    creates and noops leave it empty.
    """

    item_code: str
    sku: str | None
    proposed_name: str
    action: str  # create | update | noop | deactivate | delete
    diffs: list[FieldDiff] = field(default_factory=list)
    current_external_id: str | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "item_code": self.item_code,
            "sku": self.sku,
            "proposed_name": self.proposed_name,
            "action": self.action,
            "diffs": [d.to_dict() for d in self.diffs],
            "current_external_id": self.current_external_id,
            "notes": list(self.notes),
        }


@dataclass
class OrphanProduct:
    """A backend product with no matching Item in the configured scope.

    The form renders these as a dedicated section so the operator can
    choose ``deactivate`` or ``delete`` per row. ``suggested_action``
    mirrors the parent Sync's ``orphan_policy`` so the dialog pre-ticks
    the right radio.
    """

    external_id: str
    name: str | None
    sku: str | None
    last_modified: str | None = None
    product_count: int = 0  # unused; symmetrical to OrphanNode in catalog_mirror
    suggested_action: str = "keep"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ConflictItem:
    """An Item that multiple Syncs claim at the same priority.

    ``resolution`` mirrors the policy value that produced the conflict
    so the dialog can show "manual review required" vs "last_wins
    pending" vs "skip" without re-deriving it.
    """

    item_code: str
    competing_syncs: list[str]
    resolution: str  # manual_review | last_wins | skip

    def to_dict(self) -> dict:
        return {
            "item_code": self.item_code,
            "competing_syncs": list(self.competing_syncs),
            "resolution": self.resolution,
        }


@dataclass
class MappingDriftItem:
    """An Item whose recorded external_id no longer resolves.

    Surfaces in the preview so the operator knows the backend was
    edited from under the sync (e.g. a product hard-deleted in the
    Shopware admin UI). The differ clears the stale mapping and treats
    the Item as a fresh create on the next live run.
    """

    item_code: str
    stale_external_id: str
    proposed_name: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ProductSyncPreviewPlan:
    """Top-level preview output for one Product Sync.

    Buckets match the action codes in :mod:`constants` plus the
    informational ``noops`` bucket so the dialog can render the full
    set of products (not just the diff). ``conflicts`` and
    ``mapping_drift`` are surfaced separately because operators usually
    fix those before approving the run.
    """

    sync: str
    title: str
    backend: str
    items_in_scope: int = 0
    creates: list[ProductNodePlan] = field(default_factory=list)
    updates: list[ProductNodePlan] = field(default_factory=list)
    noops: list[ProductNodePlan] = field(default_factory=list)
    orphans: list[OrphanProduct] = field(default_factory=list)
    conflicts: list[ConflictItem] = field(default_factory=list)
    mapping_drift: list[MappingDriftItem] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    skipped: bool = False
    skip_reason: str | None = None

    def to_dict(self) -> dict:
        # We don't use ``asdict(self)`` wholesale because the nested
        # dataclasses each have their own ``to_dict`` and (in the
        # ProductNodePlan case) need a custom field shape. Doing the
        # nesting by hand keeps the JS contract explicit and avoids
        # accidental field additions leaking into the API.
        return {
            "sync": self.sync,
            "title": self.title,
            "backend": self.backend,
            "items_in_scope": self.items_in_scope,
            "creates": [n.to_dict() for n in self.creates],
            "updates": [n.to_dict() for n in self.updates],
            "noops": [n.to_dict() for n in self.noops],
            "orphans": [o.to_dict() for o in self.orphans],
            "conflicts": [c.to_dict() for c in self.conflicts],
            "mapping_drift": [m.to_dict() for m in self.mapping_drift],
            "notes": list(self.notes),
            "skipped": self.skipped,
            "skip_reason": self.skip_reason,
        }


@dataclass
class ProductSyncRunResult:
    """Outcome of a live :func:`tasks.apply_sync` run.

    Counts mirror the preview buckets; ``errors`` carries short
    operator-facing strings (full tracebacks go to ``Ecommerce
    Integration Log``).
    """

    sync: str
    backend: str
    status: str  # pending | running | ok | error | partial
    created: int = 0
    updated: int = 0
    deactivated: int = 0
    deleted: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)
    message: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)
