"""Idempotency-key rotator on the shared Shopware client.

The shared-session path in ``_with_client`` bypasses
``temp_shopware_session`` (which mints a fresh key + patches the
client per call). The rotator pattern lets us swap the key on a
pinned client without re-patching ``_get_headers`` each time —
verify here that the patch is idempotent at the client level and
the key swap fires per call.
"""

from __future__ import annotations

import unittest


class _StubClient:
    """Minimal duck-type of ``Shopware6AdminAPIClientBase`` with the
    ``_get_headers`` method the rotator wraps. Returns whatever the
    test feeds it via ``self.base_headers``."""

    def __init__(self):
        self.base_headers = {"Content-Type": "application/json"}

    def _get_headers(self, *args, **kwargs):
        return dict(self.base_headers)


class TestIdempotencyRotator(unittest.TestCase):
    def test_returns_same_holder_across_calls(self):
        from ecommerce_integrations.shopware6.connection import (
            _ensure_idempotency_rotator,
        )
        c = _StubClient()
        h1 = _ensure_idempotency_rotator(c)
        h2 = _ensure_idempotency_rotator(c)
        self.assertIs(h1, h2)

    def test_patches_get_headers_only_once(self):
        from ecommerce_integrations.shopware6.connection import (
            _ensure_idempotency_rotator,
        )
        c = _StubClient()
        original = c._get_headers
        _ensure_idempotency_rotator(c)
        patched = c._get_headers
        self.assertIsNot(patched, original)
        # Second call must not re-wrap
        _ensure_idempotency_rotator(c)
        self.assertIs(c._get_headers, patched)

    def test_key_swap_appears_in_headers(self):
        from ecommerce_integrations.shopware6.connection import (
            _ensure_idempotency_rotator,
        )
        c = _StubClient()
        holder = _ensure_idempotency_rotator(c)
        holder.key = "key-A"
        self.assertEqual(c._get_headers().get("sw-api-idempotency-key"), "key-A")
        holder.key = "key-B"
        self.assertEqual(c._get_headers().get("sw-api-idempotency-key"), "key-B")

    def test_unset_key_means_no_header(self):
        """Initially the holder has no key — headers must NOT carry
        the idempotency field. Otherwise a stale ``None`` would land
        in the request and Shopware would treat it as an explicit
        empty key."""
        from ecommerce_integrations.shopware6.connection import (
            _ensure_idempotency_rotator,
        )
        c = _StubClient()
        _ensure_idempotency_rotator(c)
        self.assertNotIn("sw-api-idempotency-key", c._get_headers())

    def test_attach_helper_uses_rotator(self):
        """``_attach_idempotency_key`` (used by ``temp_shopware_session``)
        must go through the same rotator — otherwise the patch would
        double-wrap when the temp decorator runs against an already-
        ambient client."""
        from ecommerce_integrations.shopware6.connection import (
            _attach_idempotency_key,
            _ensure_idempotency_rotator,
        )
        c = _StubClient()
        _attach_idempotency_key(c, "first")
        patched_after_first = c._get_headers
        _attach_idempotency_key(c, "second")
        self.assertIs(c._get_headers, patched_after_first)
        self.assertEqual(_ensure_idempotency_rotator(c).key, "second")
        self.assertEqual(c._get_headers().get("sw-api-idempotency-key"), "second")


if __name__ == "__main__":
    unittest.main()
