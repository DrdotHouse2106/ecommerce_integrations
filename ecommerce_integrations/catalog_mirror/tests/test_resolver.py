"""Resolver tests: real Item / Item Group / Mirror / Smart Collection.

Builds a synthetic catalog (3-level IG tree, two items, one Mirror, one
Smart Collection) directly in the DB — no mocking — and asserts the
unified resolver's behaviour for the locked Phase 2 contract:

- Variant A precedence: highest visibility wins per channel.
- Mirror channel cannot be weakened by a Smart Collection on the same
  channel.
- Per-item exclude veto suppresses a channel for that item only.
- Categories dedup by external_id, mirror source outranks SC source.
- Default channel fallback emits a warning entry when nothing matches.

Generic placeholder identifiers only (CLAUDE.md rule).
"""

from __future__ import annotations

import frappe
from frappe.tests import IntegrationTestCase

from ecommerce_integrations.catalog_mirror.resolver import (
    ChannelEntry,
    ItemResolution,
    categories_for_item,
    channels_for_item,
    invalidate_cache,
    resolve_item,
)


def _root_item_group() -> str:
    if frappe.db.exists("Item Group", "All Item Groups"):
        return "All Item Groups"
    name = frappe.db.get_value(
        "Item Group", {"is_group": 1}, "name", order_by="lft asc",
    )
    if not name:
        raise RuntimeError("No root Item Group on this site")
    return name


def _default_uom() -> str:
    for candidate in ("Nos", "Stk", "Einheit"):
        if frappe.db.exists("UOM", candidate):
            return candidate
    name = frappe.db.get_value("UOM", {}, "name")
    if not name:
        raise RuntimeError("No UOM on this site")
    return name


def _make_group(name: str, parent: str, is_group: int = 1) -> str:
    if frappe.db.exists("Item Group", name):
        return name
    frappe.get_doc({
        "doctype": "Item Group",
        "item_group_name": name,
        "parent_item_group": parent,
        "is_group": is_group,
    }).insert(ignore_permissions=True)
    return name


def _make_item(code: str, group: str) -> str:
    if frappe.db.exists("Item", code):
        return code
    frappe.get_doc({
        "doctype": "Item",
        "item_code": code,
        "item_name": code,
        "item_group": group,
        "stock_uom": _default_uom(),
        "is_sales_item": 1,
        "published_in_website": 1,
    }).insert(ignore_permissions=True)
    return code


class TestResolver(IntegrationTestCase):
    """End-to-end resolver tests.

    Setup builds a deterministic catalog so every test sees the same
    inputs. Each test calls ``invalidate_cache()`` first so it observes
    a fresh build (the resolver caches per-backend for 5 minutes — long
    enough to silently couple tests if we don't flush).
    """

    PREFIX = "TGroup-CMR"
    ITEM_PREFIX = "TItem-CMR"
    MIRROR_TITLE = "test-catalog-mirror-resolver"
    SC_SLUG = "tcoll-resolver-leaf"
    CHANNEL_A = "sc-resolver-channel-a-placeholder"
    CHANNEL_B = "sc-resolver-channel-b-placeholder"
    EXT_CAT_LEAF = "ext-cat-leaf-placeholder"
    EXT_CAT_BRANCH = "ext-cat-branch-placeholder"
    EXT_CAT_SC = "ext-cat-sc-placeholder"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._wipe()
        root_parent = _root_item_group()
        cls.root_ig = _make_group(f"{cls.PREFIX}-Root", root_parent, is_group=1)
        cls.branch = _make_group(f"{cls.PREFIX}-Branch", cls.root_ig, is_group=1)
        cls.leaf = _make_group(f"{cls.PREFIX}-Leaf", cls.branch, is_group=0)
        cls.other_leaf = _make_group(
            f"{cls.PREFIX}-Other", root_parent, is_group=0,
        )

        cls.item_in_mirror = _make_item(
            f"{cls.ITEM_PREFIX}-001", cls.leaf,
        )
        cls.item_out_of_mirror = _make_item(
            f"{cls.ITEM_PREFIX}-002", cls.other_leaf,
        )

        # Wire backend category ids onto the IG tree so the mirror layer
        # produces CategoryEntry rows.
        frappe.db.set_value(
            "Item Group", cls.leaf, "shopware_category_id", cls.EXT_CAT_LEAF,
            update_modified=False,
        )
        frappe.db.set_value(
            "Item Group", cls.branch, "shopware_category_id", cls.EXT_CAT_BRANCH,
            update_modified=False,
        )

        # The Mirror covering the leaf IG. target_sales_channel stores
        # the raw Shopware UUID via an Autocomplete field — no Frappe
        # link validation runs against it. flags.ignore_links is kept
        # defensively in case the fieldtype is ever reverted.
        mirror = frappe.get_doc({
            "doctype": "Ecommerce Catalog Mirror",
            "title": cls.MIRROR_TITLE,
            "backend": "Shopware",
            "is_active": 1,
            "root_item_group": cls.root_ig,
            "external_root_id": "live-root",
            "target_sales_channel": cls.CHANNEL_A,
            "name_template": "{{ item_group.item_group_name }}",
            "orphan_policy": "keep",
        })
        mirror.flags.ignore_links = True
        mirror.insert(ignore_permissions=True)
        cls.mirror_name = mirror.name

        # The Smart Collection: matches every item in the leaf IG, two
        # targets — one collides with the Mirror's channel at a lower
        # visibility, one targets a second channel with an external_id
        # (category source).
        coll = frappe.get_doc({
            "doctype": "Ecommerce Smart Collection",
            "title": "Resolver-Test-Collection",
            "slug": cls.SC_SLUG,
            "is_active": 1,
            "rule_combinator": "AND",
            "rules": [
                {
                    "rule_type": "Item Group",
                    "operator": "equals",
                    "value": cls.leaf,
                },
            ],
            "targets": [
                {
                    "backend": "Shopware",
                    "sales_channel": cls.CHANNEL_A,
                    "enabled": 1,
                    "visibility": "Linked Only (20)",
                },
                {
                    "backend": "Shopware",
                    "sales_channel": cls.CHANNEL_B,
                    "enabled": 1,
                    "visibility": "All (30)",
                    "external_id": cls.EXT_CAT_SC,
                },
            ],
        }).insert(ignore_permissions=True)
        cls.coll_name = coll.name

    @classmethod
    def tearDownClass(cls):
        cls._wipe()
        super().tearDownClass()

    @classmethod
    def _wipe(cls):
        for dt, filters in (
            (
                "Ecommerce Catalog Mirror",
                {"title": cls.MIRROR_TITLE},
            ),
            (
                "Ecommerce Smart Collection",
                {"slug": cls.SC_SLUG},
            ),
        ):
            for name in frappe.get_all(dt, filters=filters, pluck="name"):
                try:
                    frappe.delete_doc(
                        dt, name, force=True, ignore_permissions=True,
                    )
                except Exception:
                    pass
        for code in frappe.get_all(
            "Item",
            filters={"item_code": ("like", f"{cls.ITEM_PREFIX}%")},
            pluck="name",
        ):
            try:
                frappe.delete_doc("Item", code, force=True, ignore_permissions=True)
            except Exception:
                pass
        for name in frappe.get_all(
            "Item Group",
            filters={"name": ("like", f"{cls.PREFIX}%")},
            pluck="name",
            order_by="lft desc",
        ):
            try:
                frappe.delete_doc(
                    "Item Group", name, force=True, ignore_permissions=True,
                )
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Variant A: highest visibility wins
    # ------------------------------------------------------------------

    def test_variant_a_mirror_outranks_smart_collection_on_same_channel(self):
        """SC@20 + Mirror@30 on CHANNEL_A → one entry at 30, mirror source."""
        invalidate_cache()
        entries = channels_for_item(self.item_in_mirror, "Shopware")
        by_channel = {e.sales_channel: e for e in entries}
        self.assertIn(self.CHANNEL_A, by_channel)
        on_a = by_channel[self.CHANNEL_A]
        self.assertEqual(on_a.visibility, 30)
        self.assertTrue(
            on_a.source.startswith("mirror:"),
            f"expected mirror source for CHANNEL_A, got {on_a.source!r}",
        )

    def test_sc_only_channel_keeps_its_entry(self):
        """SC@30 on CHANNEL_B (no mirror) → entry preserved with SC source."""
        invalidate_cache()
        entries = channels_for_item(self.item_in_mirror, "Shopware")
        by_channel = {e.sales_channel: e for e in entries}
        self.assertIn(self.CHANNEL_B, by_channel)
        on_b = by_channel[self.CHANNEL_B]
        self.assertEqual(on_b.visibility, 30)
        self.assertTrue(on_b.source.startswith("smart_collection:"))

    # ------------------------------------------------------------------
    # Category dedup + source preference
    # ------------------------------------------------------------------

    def test_category_dedup_prefers_mirror_source(self):
        """Same category surfaced via both layers → one entry, mirror src."""
        invalidate_cache()
        # Pretend the SC target also names EXT_CAT_LEAF to force a clash.
        # We model the clash by patching the SC target row directly.
        frappe.db.sql(
            """UPDATE `tabEcommerce Smart Collection Target`
               SET external_id = %s
               WHERE parent = %s AND sales_channel = %s""",
            (self.EXT_CAT_LEAF, self.coll_name, self.CHANNEL_B),
        )
        try:
            invalidate_cache()
            cats = categories_for_item(self.item_in_mirror, "Shopware")
            by_ext = {c.external_id: c for c in cats}
            self.assertIn(self.EXT_CAT_LEAF, by_ext)
            self.assertTrue(
                by_ext[self.EXT_CAT_LEAF].source.startswith("mirror:"),
                f"expected mirror src, got {by_ext[self.EXT_CAT_LEAF].source!r}",
            )
        finally:
            frappe.db.sql(
                """UPDATE `tabEcommerce Smart Collection Target`
                   SET external_id = %s
                   WHERE parent = %s AND sales_channel = %s""",
                (self.EXT_CAT_SC, self.coll_name, self.CHANNEL_B),
            )
            invalidate_cache()

    def test_mirror_emits_full_category_chain(self):
        """The leaf item gets both leaf and branch category entries."""
        invalidate_cache()
        cats = categories_for_item(self.item_in_mirror, "Shopware")
        ext_ids = {c.external_id for c in cats}
        self.assertIn(self.EXT_CAT_LEAF, ext_ids)
        self.assertIn(self.EXT_CAT_BRANCH, ext_ids)

    # ------------------------------------------------------------------
    # Per-item override veto (synthetic — Phase 4 ships the real table)
    # ------------------------------------------------------------------

    def test_exclude_veto_suppresses_channel(self):
        """Synthetic override rows fed straight into the assembly stage."""
        from ecommerce_integrations.catalog_mirror.resolver import (
            _assemble_resolution,
            _resolve_mirror_for_item,
            _resolve_sc_for_item,
        )

        invalidate_cache()
        mirror_entries = _resolve_mirror_for_item(
            self.item_in_mirror, "Shopware",
        )
        sc_entries = _resolve_sc_for_item(self.item_in_mirror, "Shopware")

        # Without the override, both channels should resolve.
        baseline = _assemble_resolution(
            self.item_in_mirror, "Shopware",
            overrides=[],
            mirror_entries=mirror_entries,
            sc_entries=sc_entries,
        )
        self.assertIn(
            self.CHANNEL_A, {e.sales_channel for e in baseline.channels},
        )

        # With an exclude on CHANNEL_A, the mirror's entry must be vetoed
        # even though its visibility is higher.
        with_exclude = _assemble_resolution(
            self.item_in_mirror, "Shopware",
            overrides=[
                {
                    "sales_channel": self.CHANNEL_A,
                    "visibility": "Hidden (0)",
                    "mode": "exclude",
                    "backend": "Shopware",
                },
            ],
            mirror_entries=mirror_entries,
            sc_entries=sc_entries,
        )
        active = {e.sales_channel for e in with_exclude.channels}
        self.assertNotIn(self.CHANNEL_A, active)
        # And the excluded entry is exposed informationally.
        self.assertEqual(len(with_exclude.excluded_channels), 1)
        self.assertEqual(
            with_exclude.excluded_channels[0].sales_channel, self.CHANNEL_A,
        )

    def test_include_override_outranks_other_layers(self):
        """Override:include at any visibility outranks SC/Mirror on a tie."""
        from ecommerce_integrations.catalog_mirror.resolver import (
            _assemble_resolution,
            _resolve_mirror_for_item,
            _resolve_sc_for_item,
        )

        invalidate_cache()
        mirror_entries = _resolve_mirror_for_item(
            self.item_in_mirror, "Shopware",
        )
        sc_entries = _resolve_sc_for_item(self.item_in_mirror, "Shopware")

        # Override at visibility=30 on the mirror's channel. Tie with the
        # mirror's 30; source rank promotes override above mirror.
        res = _assemble_resolution(
            self.item_in_mirror, "Shopware",
            overrides=[
                {
                    "sales_channel": self.CHANNEL_A,
                    "visibility": "All (30)",
                    "mode": "include",
                    "backend": "Shopware",
                },
            ],
            mirror_entries=mirror_entries,
            sc_entries=sc_entries,
        )
        by_channel = {e.sales_channel: e for e in res.channels}
        self.assertEqual(by_channel[self.CHANNEL_A].source, "override:include")

    # ------------------------------------------------------------------
    # Fallback
    # ------------------------------------------------------------------

    def test_default_fallback_emits_warning_when_no_match(self):
        """Item outside every Mirror/SC → warning + (maybe) default channel."""
        invalidate_cache()
        res = resolve_item(self.item_out_of_mirror, "Shopware")
        self.assertIsInstance(res, ItemResolution)
        # No mirror, no SC, no override → the only way we get channels
        # is via the default fallback. The test site may or may not
        # have a Shopware Setting configured, so we assert the warning
        # is present in either case.
        self.assertTrue(
            res.warnings, "expected at least one warning on uncovered item",
        )

    # ------------------------------------------------------------------
    # Shape sanity
    # ------------------------------------------------------------------

    def test_resolve_item_returns_typed_dataclasses(self):
        """resolve_item returns ItemResolution; channels are ChannelEntry."""
        invalidate_cache()
        res = resolve_item(self.item_in_mirror, "Shopware")
        self.assertIsInstance(res, ItemResolution)
        for c in res.channels:
            self.assertIsInstance(c, ChannelEntry)
        # to_dict round-trips cleanly (Phase 4 calls this for the form).
        as_dict = res.to_dict()
        self.assertEqual(as_dict["item_code"], self.item_in_mirror)
        self.assertEqual(as_dict["backend"], "Shopware")
        self.assertIsInstance(as_dict["channels"], list)
        self.assertIsInstance(as_dict["categories"], list)
