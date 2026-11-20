"""``visibility_overrides_only``: an item is visible only on channels
that an explicit layer (override / Catalog Mirror / Smart Collection)
placed it on — no default-channel or target-channel fallback."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch


def _entry(channel, source):
    return SimpleNamespace(sales_channel=channel, visibility=30, source=source)


def _sync(**kw):
    base = {"backend": "Shopware", "name": "SYNC-1", "default_sales_channel": "sc-default",
            "target_sales_channels": [SimpleNamespace(sales_channel_id="sc-target")],
            "sync_visibilities": 1, "visibility_overrides_only": 0}
    base.update(kw)
    return SimpleNamespace(**base)


class TestStrictVisibility(unittest.TestCase):
    def _run(self, entries, **kw):
        from ecommerce_integrations.product_sync.engine import canonical
        with patch("ecommerce_integrations.catalog_mirror.resolver.channels_for_item",
                   return_value=entries), \
             patch.object(canonical, "_get_linked_sc_member_codes", return_value=frozenset()):
            return canonical._canonical_visibilities(SimpleNamespace(item_code="ITEM-001"), _sync(**kw))

    def test_default_mode_falls_back(self):
        self.assertEqual([e["channel_id"] for e in self._run([])], ["sc-default"])

    def test_strict_without_placement_is_hidden(self):
        self.assertEqual(self._run([], visibility_overrides_only=1), [])

    def test_strict_drops_resolver_default_fallback(self):
        self.assertEqual(self._run([_entry("sc-default", "default")], visibility_overrides_only=1), [])

    def test_strict_keeps_overrides(self):
        out = self._run([_entry("sc-b", "override:include"), _entry("sc-a", "mirror:X")],
                        visibility_overrides_only=1)
        self.assertEqual([e["channel_id"] for e in out], ["sc-a", "sc-b"])

    def test_strict_needs_sync_visibilities(self):
        from ecommerce_integrations.product_sync.tasks import _visibility_strict
        self.assertTrue(_visibility_strict(_sync(visibility_overrides_only=1)))
        self.assertFalse(_visibility_strict(_sync(visibility_overrides_only=1, sync_visibilities=0)))


class TestReconcileAllowEmpty(unittest.TestCase):
    def _client(self):
        calls = []

        class C:
            def request_post(self, path, payload=None):
                calls.append((path, payload))
                data = [{"id": "p1", "visibilities": [{"id": "v1", "salesChannelId": "sc-a", "visibility": 30}]}]
                return SimpleNamespace(data=data if path == "search/product" else [])
        return C(), calls

    def _impl(self, entries, **kw):
        from ecommerce_integrations.product_sync.engine.adapters.shopware import ShopwareProductAdapter
        client, calls = self._client()
        out = ShopwareProductAdapter._reconcile_visibilities_impl(None, client, external_id="p1", entries=entries, **kw)
        return out, calls

    def test_empty_without_flag_is_noop(self):
        out, calls = self._impl([])
        self.assertEqual(calls, [])
        self.assertEqual(out["deleted"], 0)

    def test_empty_with_flag_unlinks_all(self):
        out, calls = self._impl([], allow_empty=True)
        self.assertEqual(out["deleted"], 1)
        self.assertEqual(calls[-1][1]["visdel"]["payload"], [{"id": "v1"}])
