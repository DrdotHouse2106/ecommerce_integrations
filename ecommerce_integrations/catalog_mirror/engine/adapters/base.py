"""Backend-agnostic category-tree adapter interface for Catalog Mirror.

Each backend implements this interface and registers itself under the
backend name (``Shopware``, ``Medusa``, …). The orchestrator picks the
adapter for each mirror based on ``mirror.backend``.

Adapters are responsible for:

- fetching the live category tree under a known root (paginated)
- locating an existing backend category by name for the Adopt workflow
- creating or updating a single category (upsert)
- re-parenting a category (move)
- deleting a category (only on explicit orphan_policy=delete)

Errors are surfaced via :class:`AdapterError` so the orchestrator can
route them into ``Ecommerce Integration Log`` consistently.

The contract is intentionally *node-level* — the orchestrator owns the
tree-walk ordering (parents before children, moves bottom-up, deletes
last). Adapters do not see the whole tree at write time; this keeps
each HTTP call small, retryable and idempotent.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


class AdapterError(Exception):
    """Raised by adapters for sync-time failures (HTTP errors, mapping
    misses, etc). The orchestrator catches and logs to Ecommerce
    Integration Log; the original traceback is preserved via __cause__."""


@dataclass
class LiveCategoryNode:
    """One backend category as it exists right now.

    ``children`` is populated by :meth:`CatalogAdapter.fetch_tree` so the
    differ can walk the live tree without making any further HTTP calls.
    ``product_count`` is informational only — used by the preview to
    flag "this category has N products" before a delete.
    """

    external_id: str
    name: str
    parent_external_id: str | None
    description: str | None = None
    active: bool = True
    product_count: int = 0
    # SEO meta as it exists in the backend right now. Compared against the
    # mirror's rendered ``meta_*_template`` so drift flips an update. Left
    # ``None`` by backends without a native SEO-meta concept.
    meta_title: str | None = None
    meta_description: str | None = None
    children: list["LiveCategoryNode"] = field(default_factory=list)
    # Operator opt-out: the live category carries a backend flag
    # (Shopware ``customFields.erp_ignored = true``) marking it as
    # manually managed. The differ must never orphan (delete /
    # deactivate) or overwrite such a node — it's invisible to the
    # mirror. Inherited down the subtree by ``fetch_tree`` so a flagged
    # parent shields its children too.
    ignored: bool = False


@dataclass
class NodeMatch:
    """A candidate backend category for the Adopt workflow.

    Returned from :meth:`CatalogAdapter.find_matching_nodes` when an IG
    has no recorded ``<backend>_category_id`` but the operator suspects
    a same-named category already exists in the backend (typical on
    first-mirror over a hand-curated tree).
    """

    external_id: str
    name: str
    path: str
    linked_product_count: int = 0


class CatalogAdapter(ABC):
    backend: str = ""

    @abstractmethod
    def fetch_tree(
        self, root_external_id: str | None,
    ) -> LiveCategoryNode | None:
        """Return the full live tree under ``root_external_id``.

        Returns ``None`` when the root cannot be resolved (e.g. Shopware
        sales-channel has no navigationCategory and no explicit root id
        was provided). The orchestrator surfaces this as
        ``unresolved_external_root`` in the preview.

        Implementations should paginate. Tree depth is unbounded in
        principle but operators typically have at most 4-5 levels;
        adapters should still guard against cycles (track visited ids).
        """

    @abstractmethod
    def find_matching_nodes(
        self, name: str, parent_external_id: str | None,
    ) -> list[NodeMatch]:
        """Search the backend for categories named ``name``.

        Constrain to direct children of ``parent_external_id`` when
        supplied (the typical "adopt under this branch" case). When
        ``parent_external_id`` is ``None`` the search is global — used
        for the root match. Adapters should cap results (25 is a
        reasonable default).
        """

    @abstractmethod
    def upsert_node(
        self,
        *,
        external_id: str | None,
        parent_external_id: str | None,
        name: str,
        description: str | None,
        active: bool,
        target_sales_channel: str | None,
        meta_title: str | None = None,
        meta_description: str | None = None,
    ) -> str:
        """Create or update a single category. Return its external_id.

        When ``external_id`` is ``None`` this creates a new category
        under ``parent_external_id``. When set, the call patches the
        existing category. Implementations must be idempotent on
        repeated calls with the same inputs.

        ``target_sales_channel`` is Shopware-specific — Medusa ignores
        it. The adapter handles the assignment internally so the
        orchestrator doesn't need to know whether the backend supports
        per-channel categorisation.

        ``meta_title`` / ``meta_description`` carry the rendered SEO meta.
        Backends with native category SEO fields (Shopware
        ``metaTitle``/``metaDescription``) map them through; backends
        without may ignore them. ``None`` means "not managed by this
        mirror" — the adapter must not clear an existing backend value.
        """

    @abstractmethod
    def move_node(
        self, external_id: str, new_parent_external_id: str | None,
    ) -> None:
        """Re-parent ``external_id`` under ``new_parent_external_id``.

        Called when the differ detects an IG was re-parented in
        ERPNext. The adapter PATCHes only the parent reference — the
        full :meth:`upsert_node` path is reserved for name/description/
        active drift so reparents are observable as a distinct action
        in the integration log.
        """

    @abstractmethod
    def delete_node(self, external_id: str) -> None:
        """Delete the backend category for ``external_id``.

        Called only when ``orphan_policy=delete`` or when the operator
        explicitly invokes the per-row delete from the preview dialog.
        404 is acceptable (treated as already-deleted; idempotent).
        """


_REGISTRY: dict[str, type[CatalogAdapter]] = {}


def register(backend: str):
    def deco(cls: type[CatalogAdapter]) -> type[CatalogAdapter]:
        cls.backend = backend
        _REGISTRY[backend] = cls
        return cls

    return deco


def get_adapter(backend: str) -> CatalogAdapter:
    cls = _REGISTRY.get(backend)
    if cls is None:
        raise AdapterError(f"No adapter registered for backend {backend!r}")
    return cls()


def known_backends() -> list[str]:
    return sorted(_REGISTRY.keys())
