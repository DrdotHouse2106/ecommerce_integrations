"""Hashable canonical for a Catalog-Mirror category node.

Analogous to ``product_sync/engine/canonical.py``: the canonical is the
ERPNext-derived projection of everything the mirror pushes to a backend
category. Hashing it yields a stable per-node fingerprint so a run can
record "this node was last synced at canonical X" and skip re-pushing an
unchanged node — while the live-diff in :mod:`differ` stays authoritative
for backend-side drift (someone editing the category in the shop).

Determinism rules (mirroring the product-sync canonical):

- stable key order (``sort_keys`` at hash time),
- strings stripped; ``None`` collapses to ``""``,
- ``active`` coerced to ``bool``,
- ``PAYLOAD_VERSION`` baked in so a schema change invalidates old hashes
  wholesale — bump it whenever you add or rename a canonical field.

Parent linkage is deliberately **not** part of the canonical: re-parents
are structural and resolve against backend ids that only exist after a
create, so folding them in would make the hash unstable across the
create→map boundary. The differ detects moves independently
(``parent_changed``) from the live tree, and the hash stays a pure
ERPNext-content fingerprint.
"""

from __future__ import annotations

import hashlib
import json

# Bump when the canonical schema below changes (field added/renamed/
# semantics changed). Old stored hashes then never collide with new ones,
# forcing a one-time re-evaluation of every node.
PAYLOAD_VERSION = 1


def _norm(value: str | None) -> str:
    return (value or "").strip()


def build_category_canonical(
    *,
    name: str | None,
    description: str | None,
    meta_title: str | None,
    meta_description: str | None,
    active: bool,
) -> dict:
    """Return the ordered, normalised canonical dict for one category."""
    return {
        "v": PAYLOAD_VERSION,
        "name": _norm(name),
        "description": _norm(description),
        "meta_title": _norm(meta_title),
        "meta_description": _norm(meta_description),
        "active": bool(active),
    }


def hash_canonical(canonical: dict) -> str:
    """SHA-1 hex of the canonical with stable key order."""
    blob = json.dumps(
        canonical, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    )
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()
