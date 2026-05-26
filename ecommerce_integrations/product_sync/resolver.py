"""Per-item Sync resolver.

A single Item can be in the scope of more than one active Product Sync
(e.g. a generic "All Items" sync plus a brand-specific override sync).
The resolver decides which Sync is authoritative for an
``(item_code, backend)`` pair so the differ doesn't double-push the same
product through two pipelines.

Precedence (locked):

1. **Per-Sync Override.** If any active Sync has an Override row for the
   item with ``mode=pin``, that Sync wins immediately. ``mode=skip``
   removes the Sync from contention but is *not* a global veto — another
   Sync without a skip-override can still take ownership.

2. **Highest ``priority`` wins.** Each Sync carries an integer priority
   field; higher integer = higher precedence.

3. **Tie break** by ``conflict_policy`` of the highest-priority Sync:

   - ``manual_review`` → no Sync wins. The orchestrator routes the
     decision to the preview's ``conflicts`` bucket; the operator picks.
   - ``last_wins`` → the Sync with the most recent ``modified`` stamp
     wins. Cheap to implement, dangerous to rely on — kept for parity
     with Smart Collections so operators can do quick triage.
   - ``skip`` → no Sync wins; the Item is dropped from this run.

``find_active_syncs_covering_item`` walks each active Sync's scope
exactly once per run via the ``_SCOPE_CACHE`` below, then probes
the cached frozen sets — O(C) per item where C is the (small)
number of active candidate Syncs. The differ resets the cache
between runs so scope-doc edits show up on the next pass.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

import frappe

from ecommerce_integrations.product_sync.constants import (
    CONFLICT_LAST_WINS,
    CONFLICT_MANUAL_REVIEW,
    CONFLICT_SKIP,
    KNOWN_BACKENDS,
    OVERRIDE_DOCTYPE,
    OVERRIDE_PIN,
    OVERRIDE_SKIP,
    SYNC_DOCTYPE,
)


# ─── Doc-event scope gate ────────────────────────────────────────────


def item_is_in_any_active_sync(
    item_doc, backend: str, *, fail_open: bool = True,
) -> bool:
    """Two-stage scope gate used by ``Item.save()`` doc events.

    1. **Cheap IG pre-check** (cached per request): the Item's
       ``item_group`` must be in the union of every active Sync's
       covered IG tree. Filters ~99% of saves on out-of-scope IGs in
       O(1) after one cached walk per request and backend.
    2. **Authoritative resolver** (only on cache hit): confirms via
       :func:`find_active_syncs_covering_item`.

    ``fail_open=True`` keeps the legacy "queue everything" behaviour
    when any of the cache reads explode — better to push too much than
    silently swallow legitimate Item changes on transient DB hiccups.
    """
    item_code = getattr(item_doc, "name", None) or getattr(item_doc, "item_code", None)
    if not item_code:
        return fail_open
    item_group = getattr(item_doc, "item_group", None)
    if not item_group:
        return fail_open
    try:
        covered = _cached_active_scope_igs(backend)
    except Exception:
        return fail_open
    if item_group not in covered:
        return False
    try:
        return bool(find_active_syncs_covering_item(item_code, backend))
    except Exception:
        return fail_open


_SCOPE_IGS_CACHE_KEY = "_psync_scope_igs_by_backend"


def _cached_active_scope_igs(backend: str):
    """Return a container with ``__contains__`` over IG names that
    every active Sync covers. Cached on ``frappe.local.cache`` so a
    burst of Item saves only pays for the walk once per request.
    """
    cache_bucket = getattr(frappe.local, "cache", None)
    if cache_bucket is None:
        return _compute_active_scope_igs(backend)
    if _SCOPE_IGS_CACHE_KEY not in cache_bucket:
        cache_bucket[_SCOPE_IGS_CACHE_KEY] = {}
    by_backend = cache_bucket[_SCOPE_IGS_CACHE_KEY]
    if backend not in by_backend:
        by_backend[backend] = _compute_active_scope_igs(backend)
    return by_backend[backend]


def _compute_active_scope_igs(backend: str):
    """Walk every active Sync's IG tree; return the union as a set.

    Unbounded scope modes (``All`` / ``Smart Collection`` /
    ``Custom Filter``) short-circuit the gate by returning a sentinel
    whose ``__contains__`` is always True.
    """
    syncs = frappe.get_all(
        SYNC_DOCTYPE,
        filters={"is_active": 1, "backend": backend},
        fields=[
            "name", "scope_mode", "root_item_group",
            "linked_catalog_mirror", "include_descendants",
        ],
        limit=0,
    )
    if not syncs:
        return set()
    unbounded = {"All", "Smart Collection", "Custom Filter"}
    if any(s.get("scope_mode") in unbounded for s in syncs):
        return _ALL_IGS_SENTINEL

    covered: set[str] = set()
    seen_roots: set[str] = set()
    for sync in syncs:
        mode = sync.get("scope_mode")
        if mode == "Item Group Subtree":
            root = (sync.get("root_item_group") or "").strip()
            if not root or root in seen_roots:
                continue
            seen_roots.add(root)
            if int(sync.get("include_descendants") or 0):
                covered.update(_item_group_subtree(root))
            else:
                covered.add(root)
        elif mode == "Catalog Mirror":
            mirror = (sync.get("linked_catalog_mirror") or "").strip()
            if not mirror:
                continue
            root = frappe.db.get_value(
                "Ecommerce Catalog Mirror", mirror, "root_item_group",
            )
            if not root or root in seen_roots:
                continue
            seen_roots.add(root)
            covered.update(_item_group_subtree(root))
    return covered


def _item_group_subtree(root_ig: str) -> set[str]:
    """Nested-set walk for one IG root — one indexed query."""
    rows = frappe.db.sql(
        """SELECT child.name
           FROM `tabItem Group` AS root
           INNER JOIN `tabItem Group` AS child
             ON child.lft >= root.lft AND child.rgt <= root.rgt
           WHERE root.name = %s""",
        (root_ig,),
        pluck=True,
    )
    return set(rows) if rows else {root_ig}


class _AllItemGroups:
    """Sentinel: ``__contains__`` is always True. Lets callers treat
    "unbounded scope" the same shape as "explicit IG set"."""

    __slots__ = ()

    def __contains__(self, _: object) -> bool:
        return True


_ALL_IGS_SENTINEL = _AllItemGroups()


# ---------------------------------------------------------------------------
# Public dataclass
# ---------------------------------------------------------------------------


@dataclass
class ProductDecision:
    """Resolver decision for one Item × backend.

    Exactly one of these states applies, encoded in ``source``:

    - ``sync_name`` set, ``source='priority'`` → clear winner by
      priority comparison; normal push.
    - ``sync_name`` set, ``source='override'`` → a Sync's ``mode=pin``
      override took the decision; bypasses priority.
    - ``sync_name`` set, ``source='single'`` → only one Sync covers
      this Item; no comparison was needed.
    - ``sync_name`` ``None``, ``source='conflict'`` → multiple Syncs at
      the same top priority and the policy is ``manual_review``. The
      orchestrator surfaces these in the preview's conflicts bucket.
    - ``sync_name`` ``None``, ``source='skipped'`` → either every
      candidate Sync has an Override with ``mode=skip`` for this Item,
      or no active Sync covers it at all, or the tie-break resolved to
      ``skip``.

    ``competing_syncs`` carries the names of all Syncs that *could* have
    taken the Item — handy in conflict cases for the UI to render a
    "decide which to keep" picker.
    """

    item_code: str
    backend: str
    sync_name: str | None
    source: str  # priority | override | single | conflict | skipped
    competing_syncs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def resolve_item(item_code: str, backend: str) -> ProductDecision:
    """Run the precedence chain for one Item × backend.

    See the module docstring for the precedence rules. The function is
    *not* cached — caller is expected to batch lookups when running over
    many items (Phase 5 will add an index-backed bulk variant).
    """
    if backend not in KNOWN_BACKENDS:
        raise ValueError(
            f"Unknown backend {backend!r}; expected one of {KNOWN_BACKENDS}"
        )

    candidates = find_active_syncs_covering_item(item_code, backend)

    # Layer 1: overrides.
    pinned, skipped_syncs = _classify_overrides(item_code, candidates)
    if pinned:
        return ProductDecision(
            item_code=item_code,
            backend=backend,
            sync_name=pinned,
            source="override",
            competing_syncs=list(candidates),
        )

    remaining = [s for s in candidates if s not in skipped_syncs]

    if not remaining:
        # Either no Sync ever covered this Item, or every candidate
        # explicitly skipped it via an Override. Either way the
        # decision is "do not push this Item to this backend".
        return ProductDecision(
            item_code=item_code,
            backend=backend,
            sync_name=None,
            source="skipped",
            competing_syncs=list(candidates),
        )

    if len(remaining) == 1:
        return ProductDecision(
            item_code=item_code,
            backend=backend,
            sync_name=remaining[0],
            source="single",
            competing_syncs=list(candidates),
        )

    # Layer 2: priority comparison.
    rows = _load_sync_meta(remaining)
    rows.sort(
        key=lambda r: (int(r["priority"] or 0), r["modified"] or ""),
        reverse=True,
    )
    top_priority = int(rows[0]["priority"] or 0)
    top = [r for r in rows if int(r["priority"] or 0) == top_priority]

    if len(top) == 1:
        return ProductDecision(
            item_code=item_code,
            backend=backend,
            sync_name=top[0]["name"],
            source="priority",
            competing_syncs=list(candidates),
        )

    # Layer 3: tie-break via conflict_policy of the top-priority Sync.
    # When multiple top syncs disagree on policy we honour the highest
    # priority one; ``rows`` is already sorted so ``top[0]`` is also
    # the most recently modified at this priority level.
    policy = (top[0].get("conflict_policy") or CONFLICT_MANUAL_REVIEW).strip()

    if policy == CONFLICT_LAST_WINS:
        # ``rows`` is sorted by (priority desc, modified desc) so the
        # first row at this priority is already the most recent.
        return ProductDecision(
            item_code=item_code,
            backend=backend,
            sync_name=top[0]["name"],
            source="priority",
            competing_syncs=[r["name"] for r in top],
        )
    if policy == CONFLICT_SKIP:
        return ProductDecision(
            item_code=item_code,
            backend=backend,
            sync_name=None,
            source="skipped",
            competing_syncs=[r["name"] for r in top],
        )

    # Default / explicit manual_review — surface the conflict so the
    # preview's conflicts bucket lights up.
    return ProductDecision(
        item_code=item_code,
        backend=backend,
        sync_name=None,
        source="conflict",
        competing_syncs=[r["name"] for r in top],
    )


# Process-local scope cache, populated lazily and shared across all
# ``resolve_item`` calls within one differ run. Keys are Sync names,
# values are the frozen set of item_codes that fall in that Sync's
# scope. Without this cache ``find_active_syncs_covering_item`` was
# O(N × walk(sync)) per call — for a 37k-item catalogue with one
# active Sync that's ~1.4B inner iterations per full diff, which is
# why cron-triggered apply runs were crawling at <1 item/sec.
_SCOPE_CACHE: dict[str, frozenset[str]] = {}
_CANDIDATES_CACHE: dict[str, list[str]] = {}


def clear_scope_cache() -> None:
    """Invalidate the resolver's process-local scope cache.

    The differ calls this at the top of every run so a Sync whose
    scope changed between runs picks up the new membership. Within
    one run the cache is safe to share — Sync scopes don't shift
    mid-run.
    """
    _SCOPE_CACHE.clear()
    _CANDIDATES_CACHE.clear()


def _scope_set(sync_name: str) -> frozenset[str]:
    """Return the frozen set of item_codes covered by ``sync_name``.

    Lazily populates ``_SCOPE_CACHE``. Walking a Sync's scope is the
    expensive operation we want to do once per Sync per run, not once
    per (item, Sync) pair.
    """
    cached = _SCOPE_CACHE.get(sync_name)
    if cached is not None:
        return cached
    from ecommerce_integrations.product_sync.walker import walk_items_for_sync
    try:
        sync = frappe.get_doc(SYNC_DOCTYPE, sync_name)
    except Exception:  # noqa: BLE001
        result: frozenset[str] = frozenset()
    else:
        result = frozenset(n.item_code for n in walk_items_for_sync(sync))
    _SCOPE_CACHE[sync_name] = result
    return result


def find_active_syncs_covering_item(item_code: str, backend: str) -> list[str]:
    """Return the names of every active Sync whose scope covers ``item_code``.

    Uses the process-local ``_SCOPE_CACHE`` so the per-Sync walk runs
    at most once per run regardless of how many items the differ
    classifies. Callers that mutate Sync scopes should invoke
    :func:`clear_scope_cache` before the next dispatch.
    """
    if not frappe.db.exists("DocType", SYNC_DOCTYPE):
        return []

    candidates = _CANDIDATES_CACHE.get(backend)
    if candidates is None:
        candidates = frappe.get_all(
            SYNC_DOCTYPE,
            filters={"is_active": 1, "backend": backend},
            pluck="name",
        )
        _CANDIDATES_CACHE[backend] = candidates

    if not candidates:
        return []

    return [name for name in candidates if item_code in _scope_set(name)]


# ---------------------------------------------------------------------------
# Override + meta helpers
# ---------------------------------------------------------------------------


def _classify_overrides(
    item_code: str, candidate_sync_names: list[str],
) -> tuple[str | None, set[str]]:
    """Walk the Override child table and split into ``(pinned, skipped)``.

    ``pinned`` is the name of the Sync that pinned this item (first
    match wins — overrides aren't supposed to disagree). ``skipped`` is
    the set of Sync names that explicitly skipped the item.

    Other modes (``custom``) are not treated as a precedence override
    here: they affect the *payload* the differ emits, not the *which
    Sync wins* question. Phase 3's differ reads them directly.
    """
    if not candidate_sync_names:
        return None, set()
    if not frappe.db.exists("DocType", OVERRIDE_DOCTYPE):
        return None, set()

    rows = frappe.db.sql(
        f"""
        SELECT parent AS sync_name, mode
        FROM `tab{OVERRIDE_DOCTYPE}`
        WHERE parenttype = %(parenttype)s
          AND item_code = %(item_code)s
          AND parent IN %(syncs)s
        """,
        {
            "parenttype": SYNC_DOCTYPE,
            "item_code": item_code,
            "syncs": tuple(candidate_sync_names),
        },
        as_dict=True,
    )

    pinned: str | None = None
    skipped: set[str] = set()
    for row in rows:
        mode = (row.get("mode") or "").strip().lower()
        sync_name = row.get("sync_name")
        if not sync_name:
            continue
        if mode == OVERRIDE_PIN and pinned is None:
            pinned = sync_name
        elif mode == OVERRIDE_SKIP:
            skipped.add(sync_name)
    return pinned, skipped


def _load_sync_meta(sync_names: list[str]) -> list[dict]:
    """Load the priority/policy/modified metadata for a list of Syncs.

    Returned as a list of dicts so the caller can sort in-place without
    juggling per-row Document fetches.
    """
    if not sync_names:
        return []
    rows = frappe.db.sql(
        f"""
        SELECT name, priority, conflict_policy, modified
        FROM `tab{SYNC_DOCTYPE}`
        WHERE name IN %(names)s
        """,
        {"names": tuple(sync_names)},
        as_dict=True,
    )
    # Coerce to plain dicts so callers can mutate freely (the sort key
    # below appends derived fields in some Phase 3 code paths).
    return [dict(r) for r in rows]
