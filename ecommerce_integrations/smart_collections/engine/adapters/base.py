"""Backend-agnostic category adapter interface for Smart Collections sync.

Each backend implements this interface and registers itself under the
backend name (``Medusa``, ``Shopware``, …). The orchestrator picks the
adapter for each Target based on ``target.backend``.

Adapters are responsible for:
- creating or updating the backend's category for a Smart Collection
- linking / unlinking items to that category
- never deleting the category implicitly (only on explicit collection
  deletion — see spec §3.1)

Errors are surfaced via ``AdapterError`` so the orchestrator can route
them into ``Ecommerce Integration Log`` consistently.
"""

from abc import ABC, abstractmethod


class AdapterError(Exception):
    """Raised by adapters for sync-time failures (HTTP errors, mapping
    misses, etc). The orchestrator catches and logs to Ecommerce
    Integration Log; the original traceback is preserved via __cause__."""


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
