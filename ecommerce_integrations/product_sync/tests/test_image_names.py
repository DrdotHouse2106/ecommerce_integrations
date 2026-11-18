"""SEO file names / alt texts for pushed images from Sync templates."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch


def _render(tpl, ctx):
    return tpl.replace("{{ item.item_name }}", ctx["item"]["item_name"]).replace(
        "{{ item.item_code }}", ctx["item"]["item_code"]).replace("{{ n }}", str(ctx["n"]))


class TestImageNames(unittest.TestCase):
    def _name(self, sync, images):
        import frappe

        from ecommerce_integrations.product_sync.engine import canonical
        item = SimpleNamespace(as_dict=lambda: {"item_code": "ITEM-001", "item_name": "Kotflügel vorne Citroën"})
        with patch.object(frappe, "render_template", side_effect=_render, create=True):
            canonical._name_images(images, item, sync)
        return images

    def test_without_templates_images_unchanged(self):
        imgs = self._name(SimpleNamespace(), [{"url": "/files/a.jpg", "primary": True}])
        self.assertEqual(imgs, [{"url": "/files/a.jpg", "primary": True}])

    def test_file_and_alt(self):
        sync = SimpleNamespace(image_filename_template="{{ item.item_name }}-{{ item.item_code }}",
                               image_alt_template="{{ item.item_name }}")
        imgs = self._name(sync, [{"url": "/files/a.jpg"}, {"url": "/files/b.jpg"}])
        self.assertEqual(imgs[0]["file"], "kotfluegel-vorne-citroen-item-001")
        self.assertEqual(imgs[1]["file"], "kotfluegel-vorne-citroen-item-001-2")
        self.assertEqual(imgs[0]["alt"], "Kotflügel vorne Citroën")

    def test_payload_carries_names(self):
        from ecommerce_integrations.product_sync.engine import payload
        with patch.object(payload, "_resolve_image_public_base", return_value="https://example.com"):
            out = payload._build_shopware_media([{"url": "/files/a.jpg", "file": "x", "alt": "X"}, {"url": "/files/b.jpg"}])
        self.assertEqual(out[0]["media"]["file"], "x")
        self.assertNotIn("file", out[1]["media"])


class TestApplyMediaName(unittest.TestCase):
    def _client(self, fail_first=False):
        calls = []

        class Client:
            def request_patch(self, path, payload=None):
                calls.append(("patch", path, payload))

            def request_post(self, path, payload=None):
                calls.append(("post", path, payload))
                if fail_first and len([c for c in calls if c[0] == "post"]) == 1:
                    raise Exception("CONTENT__MEDIA_DUPLICATED_FILE_NAME")

        return Client(), calls

    def test_rename_and_alt(self):
        from ecommerce_integrations.product_sync.engine.adapters.shopware import _apply_media_name
        c, calls = self._client()
        _apply_media_name(c, "abcdef1234", "img-4711", {"file": "neu", "alt": "Neu"})
        self.assertIn(("patch", "media/abcdef1234", {"alt": "Neu", "title": "Neu"}), calls)
        self.assertIn(("post", "_action/media/abcdef1234/rename", {"fileName": "neu"}), calls)

    def test_same_name_is_noop_and_duplicate_gets_suffix(self):
        from ecommerce_integrations.product_sync.engine.adapters.shopware import _apply_media_name
        c, calls = self._client()
        _apply_media_name(c, "abcdef1234", "neu", {"file": "neu"})
        self.assertEqual(calls, [])
        c, calls = self._client(fail_first=True)
        _apply_media_name(c, "abcdef1234", "alt", {"file": "neu"})
        self.assertEqual(calls[-1][2], {"fileName": "neu-abcdef12"})
