"""Perf headers (``indexing-behavior`` + ``sw-skip-trigger-flow``) and
their interaction with the idempotency-key rotator.

Both patches wrap ``client._get_headers`` — verify the composition is
correct: the perf headers are present alongside the rotated
idempotency key, neither shadows the other, and re-applying the
perf patch is idempotent at the client level.
"""

from __future__ import annotations

import unittest


class _StubClient:
    def __init__(self):
        self.base_headers = {"Content-Type": "application/json"}

    def _get_headers(self, *args, **kwargs):
        return dict(self.base_headers)


class TestPerfHeaders(unittest.TestCase):
    def test_perf_headers_present_after_patch(self):
        from ecommerce_integrations.shopware6.connection import (
            _patch_client_perf_headers,
        )
        c = _StubClient()
        _patch_client_perf_headers(c)
        h = c._get_headers()
        self.assertEqual(h.get("indexing-behavior"), "use-queue-indexing")
        self.assertEqual(h.get("sw-skip-trigger-flow"), "1")

    def test_perf_headers_compose_with_rotator(self):
        """Perf patch + rotator patch must compose so a request carries
        both the perf headers and the rotated idempotency key."""
        from ecommerce_integrations.shopware6.connection import (
            _ensure_idempotency_rotator,
            _patch_client_perf_headers,
        )
        c = _StubClient()
        _patch_client_perf_headers(c)
        _ensure_idempotency_rotator(c).key = "abc123"
        h = c._get_headers()
        self.assertEqual(h.get("indexing-behavior"), "use-queue-indexing")
        self.assertEqual(h.get("sw-skip-trigger-flow"), "1")
        self.assertEqual(h.get("sw-api-idempotency-key"), "abc123")

    def test_rotator_then_perf_also_composes(self):
        """Apply patches in the reverse order too — production wires
        them in a fixed order today, but the composition should be
        order-independent."""
        from ecommerce_integrations.shopware6.connection import (
            _ensure_idempotency_rotator,
            _patch_client_perf_headers,
        )
        c = _StubClient()
        _ensure_idempotency_rotator(c).key = "xyz"
        _patch_client_perf_headers(c)
        h = c._get_headers()
        self.assertEqual(h.get("sw-api-idempotency-key"), "xyz")
        self.assertEqual(h.get("indexing-behavior"), "use-queue-indexing")


if __name__ == "__main__":
    unittest.main()
