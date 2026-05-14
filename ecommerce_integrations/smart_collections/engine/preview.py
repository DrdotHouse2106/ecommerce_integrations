"""Dry-run preview dataclasses for Smart Collections sync.

A ``CollectionSyncPlan`` describes — without any side effects — what
would happen if the orchestrator ran :func:`tasks.sync_collection` on
this collection right now. The plan is built by
:func:`tasks.sync_collection` when called with ``dry_run=True`` and
shipped to the form via the :func:`api.preview_collection` endpoint.

Why this exists as its own module:

- The contract between the JS dialog and the Python orchestrator is
  serialisable JSON. Plain dicts work but lose all type information;
  dataclasses with ``to_dict()`` make the shape explicit and let the
  tests assert on attribute names rather than dict keys.
- The plan crosses one network boundary (Python → JS) plus optionally
  another (Python → adapter HTTP) — keeping it in one place makes it
  easy to extend (e.g. adding a ``warnings`` field later) without
  hunting through ``tasks.py``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class TargetSyncPlan:
    """What would happen to one (collection, target) pair on a live sync.

    Per-target sections:

    - ``action`` is one of ``create`` / ``update`` / ``noop``.
      ``create`` covers both "no external_id yet" and "external_id is
      stale" (the backend returned 404 on fetch). ``update`` means a
      live category exists and its name/description/active/sales-channel
      assignments differ from what the collection now wants.
    - ``to_add`` / ``to_remove`` / ``to_keep`` are item codes (the
      ERPNext side of the mapping) — the JS resolves counts and a
      first-100 sample for the dialog.
    - ``unresolved_items`` are item codes the resolver picked up but
      that have no row in ``tabEcommerce Item`` for this backend yet
      (they'll be retried after the next product export).
    - ``drift_local_only`` / ``drift_backend_only`` is the live diff
      against ``fetch_state`` — items in the local snapshot but not in
      the backend (deleted by a storefront operator) and vice-versa
      (added in the backend but not by the resolver).
    - ``match_suggestions`` is populated only when ``external_id`` is
      empty — adapter found existing same-name categories the user
      could ``Adopt`` instead of creating a new one.
    - ``link_only`` mirrors ``target.link_only`` — when ``1``, the
      live sync would *not* unlink anything from the backend even
      though ``to_remove`` is non-empty.
    """

    target_idx: int
    target_name: str
    backend: str
    sales_channel: str
    enabled: int
    link_only: int

    action: str  # "create" | "update" | "noop"
    external_id: str | None
    live_exists: bool

    proposed_name: str
    proposed_description: str
    proposed_active: int

    live_name: str | None = None
    live_description: str | None = None
    live_active: int | None = None
    live_sales_channel_ids: list[str] = field(default_factory=list)

    to_add: list[str] = field(default_factory=list)
    to_remove: list[str] = field(default_factory=list)
    to_keep: list[str] = field(default_factory=list)
    unresolved_items: list[str] = field(default_factory=list)

    drift_local_only: list[str] = field(default_factory=list)
    drift_backend_only_count: int = 0

    match_suggestions: list[dict] = field(default_factory=list)

    # When ``link_only=1`` the orchestrator will *report* ``to_remove``
    # but skip the unlink calls. The flag is mirrored here so the JS
    # can render "(SKIPPED — link_only)" instead of hiding the count
    # entirely — operator visibility is the point.
    to_remove_skipped: bool = False

    # Sales-channel diff (Shopware only). Empty list on Medusa.
    sales_channels_to_add: list[str] = field(default_factory=list)
    sales_channels_to_remove: list[str] = field(default_factory=list)

    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CollectionSyncPlan:
    """Top-level dry-run output for one collection.

    Wraps a per-target ``TargetSyncPlan`` list plus the resolver
    summary (count, sample) so the form can render the whole preview
    from a single API call.
    """

    collection: str
    title: str
    is_active: int
    skipped: bool = False
    skip_reason: str | None = None

    resolved_count: int = 0
    sample_items: list[dict] = field(default_factory=list)

    targets: list[TargetSyncPlan] = field(default_factory=list)

    def to_dict(self) -> dict:
        data = asdict(self)
        # ``asdict`` already converts the nested TargetSyncPlan objects.
        return data
