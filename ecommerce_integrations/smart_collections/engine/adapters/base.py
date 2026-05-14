"""Backend-agnostic category adapter interface for Smart Collections sync.

Each backend implements this interface and registers itself under the
backend name (``Medusa``, ``Shopware``, …). The orchestrator picks the
adapter for each Target based on ``target.backend``.

Adapters are responsible for:
- creating or updating the backend's category for a Smart Collection
- linking / unlinking items to that category
- never deleting the category implicitly (only on explicit collection
  deletion — see spec §3.1)
- reporting **live backend state** so the dry-run preview can show a
  real diff (not just a diff against the last local snapshot)
- locating an existing backend category by name (or handle, in Medusa's
  case) so the form can offer an ``Adopt`` flow that wires up a target
  to an already-curated category without recreating it.

Errors are surfaced via ``AdapterError`` so the orchestrator can route
them into ``Ecommerce Integration Log`` consistently.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


class AdapterError(Exception):
    """Raised by adapters for sync-time failures (HTTP errors, mapping
    misses, etc). The orchestrator catches and logs to Ecommerce
    Integration Log; the original traceback is preserved via __cause__."""


@dataclass
class LiveCategoryState:
    """Snapshot of a backend category as it exists right now.

    Adapters return this from :meth:`CategoryAdapter.fetch_state` so the
    dry-run preview can compute the real diff against the live backend
    rather than the local snapshot (the local snapshot can drift if a
    storefront operator hand-edits the category in the backend UI).

    ``exists=False`` is the canonical "category not in backend" state —
    used when ``target.external_id`` is empty or points at a stale id
    (the backend returned 404).
    """

    exists: bool
    external_id: str | None = None
    name: str | None = None
    description: str | None = None
    active: bool = True
    sales_channel_ids: list[str] = field(default_factory=list)
    linked_product_ids: set[str] = field(default_factory=set)
    product_count_total: int = 0


@dataclass
class CategoryMatch:
    """A candidate backend category for the adopt flow.

    Adapters return zero or more of these from
    :meth:`CategoryAdapter.find_matching_category` when the target has
    no ``external_id`` set yet. The form renders them with an ``Adopt``
    button so the user can wire up an already-curated category instead
    of creating a new one.
    """

    external_id: str
    name: str
    path: str
    has_target_sales_channel: bool
    linked_product_count: int


class CategoryAdapter(ABC):
    backend: str = ""

    @abstractmethod
    def upsert_category(self, collection, target) -> str:
        """Create or update the backend category. Return the external id.

        Idempotent: a non-empty ``target.external_id`` triggers an update;
        otherwise this creates a new category and the caller persists the
        returned id back onto ``target.external_id``.
        """

    @abstractmethod
    def link_items(self, target, item_codes: list[str]) -> list[str]:
        """Add ``item_codes`` to the target's category.

        Returns the list of item codes that the backend could not resolve
        to a product (not yet synced or removed). Adapter logs each as a
        warning; orchestrator does not treat unresolved codes as a hard
        error.
        """

    @abstractmethod
    def unlink_items(self, target, item_codes: list[str]) -> None:
        """Remove ``item_codes`` from the target's category."""

    @abstractmethod
    def delete_category(self, target) -> None:
        """Delete the backend category. Only called on explicit collection
        deletion or target removal — not on ``is_active=0``."""

    @abstractmethod
    def fetch_state(self, target) -> LiveCategoryState:
        """Read the live state of the backend category for ``target``.

        Returns ``LiveCategoryState(exists=False)`` when
        ``target.external_id`` is empty or points at a category that no
        longer exists in the backend (HTTP 404). Otherwise returns the
        category's current name/description/active flag, its sales
        channel associations (Shopware) and the set of product ids
        currently linked to it.

        This is the source of truth for the dry-run preview's "live
        diff" view — local snapshot drift relative to the backend is
        what makes operator-side hand-edits in the storefront UI come
        out as warnings instead of silent overwrites.
        """

    @abstractmethod
    def find_matching_category(self, collection, target) -> list["CategoryMatch"]:
        """Look up existing backend categories that this collection could adopt.

        Called when ``target.external_id`` is empty. The form uses the
        returned matches to offer an ``Adopt`` / ``Adopt + Link Only``
        flow — the user picks an existing manually-curated category and
        the target gets wired up to it without creating a duplicate.
        """


_REGISTRY: dict[str, type[CategoryAdapter]] = {}


def register(backend: str):
    def deco(cls: type[CategoryAdapter]) -> type[CategoryAdapter]:
        cls.backend = backend
        _REGISTRY[backend] = cls
        return cls

    return deco


def get_adapter(backend: str) -> CategoryAdapter:
    cls = _REGISTRY.get(backend)
    if cls is None:
        raise AdapterError(f"No adapter registered for backend {backend!r}")
    return cls()


def known_backends() -> list[str]:
    return sorted(_REGISTRY.keys())
