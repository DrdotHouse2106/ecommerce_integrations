"""Dataclasses describing the dry-run preview of a Catalog Mirror sync.

A :class:`MirrorPreviewPlan` describes — without side effects — what
:func:`tasks.apply_mirror` *would* do if invoked live. The plan is
serialisable JSON, returned by ``api.preview_mirror`` and rendered by
the form's *Preview Mirror* dialog (Phase 3).

The shape intentionally mirrors :mod:`smart_collections.engine.preview`
(same ``to_dict``, same field/dataclass split, same ``notes`` escape
hatch) so the JS overlay can reuse most of the existing rendering code.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class NodePlan:
    """What would happen to one ERPNext Item Group during the mirror.

    The dataclass is intentionally narrow — only the fields the JS
    dialog needs to render a row. Counts and live attributes are
    pre-resolved so the form renders without a second round-trip.
    """

    item_group: str
    item_group_path: str

    action: str  # "create" | "update" | "move" | "noop"

    proposed_name: str
    proposed_parent_external_id: str | None
    proposed_description: str | None
    proposed_active: bool

    proposed_meta_title: str | None = None
    proposed_meta_description: str | None = None

    current_external_id: str | None = None
    current_name: str | None = None
    current_parent_external_id: str | None = None
    current_description: str | None = None
    current_active: bool | None = None
    current_meta_title: str | None = None
    current_meta_description: str | None = None

    children_count: int = 0
    product_count: int = 0

    # Canonical fingerprint of the proposed content (name + description +
    # SEO meta + active). ``hash_stale`` flags a node whose live payload
    # already matches but whose *recorded* hash is out of date — the
    # orchestrator refreshes the stored hash without a backend write.
    proposed_canonical_hash: str | None = None
    hash_stale: bool = False

    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class OrphanNode:
    """A backend category under the mirror root with no matching IG.

    The form renders these as a dedicated section so the operator can
    choose ``delete now`` (one-shot via ``set_orphan_action``) or
    ``adopt to IG`` (wire it to an existing IG via ``adopt_node``).
    ``suggested_action`` mirrors the orphan_policy of the parent mirror
    so the dialog can pre-tick the right radio.
    """

    external_id: str
    name: str
    path: str
    product_count: int = 0
    last_modified: str | None = None
    suggested_action: str = "keep"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class MappingDriftNode:
    """An IG whose recorded ``<backend>_category_id`` no longer resolves.

    The orchestrator clears the mapping and treats the IG as a fresh
    create on the next live run — but the preview surfaces the drift so
    the operator knows their backend was edited out from under the
    mirror (e.g. a category deleted in the Shopware admin UI).
    """

    item_group: str
    stale_external_id: str
    proposed_name: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class MirrorPreviewPlan:
    """Top-level preview output for one Catalog Mirror.

    Buckets match :class:`TreeDiff` from :mod:`differ` 1:1 plus the
    informational ``noops`` bucket so the dialog can render the full
    tree (not just the diff) — operators ask for "show me what's in
    sync" as often as "show me the changes".
    """

    mirror: str
    title: str
    backend: str
    target_sales_channel: str | None
    root_item_group: str
    external_root_id: str | None

    is_active: int = 1
    skipped: bool = False
    skip_reason: str | None = None
    unresolved_external_root: bool = False

    creates: list[NodePlan] = field(default_factory=list)
    updates: list[NodePlan] = field(default_factory=list)
    moves: list[NodePlan] = field(default_factory=list)
    noops: list[NodePlan] = field(default_factory=list)
    orphans: list[OrphanNode] = field(default_factory=list)
    mapping_drift: list[MappingDriftNode] = field(default_factory=list)

    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        # asdict recurses into the nested dataclasses so the consumer
        # receives plain JSON-serialisable dicts all the way down.
        return asdict(self)


@dataclass
class MirrorRunResult:
    """Outcome of a live :func:`tasks.apply_mirror` run.

    Counts mirror the preview buckets; ``errors`` carries short
    operator-facing messages (full tracebacks go to the integration
    log).
    """

    mirror: str
    backend: str
    status: str  # "ok" | "error" | "skipped"

    created: int = 0
    updated: int = 0
    moved: int = 0
    deleted: int = 0
    skipped: int = 0

    skip_reason: str | None = None
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)
