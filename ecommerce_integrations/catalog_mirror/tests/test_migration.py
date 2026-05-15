"""Migration patch tests: SC umbrellas → Catalog Mirror.

Synthetic data only — no real shop identifiers, no real channel ids.
Each test builds a deterministic SC + target shape, runs the patch's
``execute()``, and asserts the resulting Mirror + SC-target state.

Covers:

- Happy path: one umbrella SC with one Item Group rule + one enabled
  Shopware target → one Catalog Mirror created, SC target disabled with
  breadcrumb on ``last_error``.
- Idempotency: re-running the patch finds the existing Mirror and
  does not create a duplicate.
- Multi-rule SC: skipped with a warning, no Mirror created.
- No Shopware target: skipped with no warning (the SC isn't an
  umbrella to begin with).
- Pre-existing Mirror that matches the (backend, channel, root_ig)
  triple: skipped as already-migrated.

Generic placeholder identifiers throughout (CLAUDE.md rule).
"""

from __future__ import annotations

import frappe
from frappe.tests import IntegrationTestCase

from ecommerce_integrations.patches.migrate_shopware_umbrellas_to_catalog_mirror import (
    execute as migrate_execute,
)

_MIRROR_DOCTYPE = "Ecommerce Catalog Mirror"
_SC_DOCTYPE = "Ecommerce Smart Collection"


def _root_item_group() -> str:
    if frappe.db.exists("Item Group", "All Item Groups"):
        return "All Item Groups"
    name = frappe.db.get_value(
        "Item Group", {"is_group": 1}, "name", order_by="lft asc",
    )
    if not name:
        raise RuntimeError("No root Item Group on this site")
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


class TestMigrateShopwareUmbrellas(IntegrationTestCase):
    """Patch behaviour against synthetic Smart Collections."""

    PREFIX = "TGroup-CMRMIG"
    SC_SLUG_PREFIX = "tcoll-cmrmig"
    MIRROR_TITLE_PREFIX = "test-cmrmig"
    CHANNEL_A = "sc-cmrmig-channel-a-placeholder"
    CHANNEL_B = "sc-cmrmig-channel-b-placeholder"
    CHANNEL_C = "sc-cmrmig-channel-c-placeholder"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._wipe()
        root_parent = _root_item_group()
        cls.root_ig = _make_group(
            f"{cls.PREFIX}-Root", root_parent, is_group=1,
        )
        cls.branch = _make_group(
            f"{cls.PREFIX}-Branch", cls.root_ig, is_group=1,
        )

    @classmethod
    def tearDownClass(cls):
        cls._wipe()
        super().tearDownClass()

    @classmethod
    def _wipe(cls):
        for dt, filters in (
            (
                _MIRROR_DOCTYPE,
                {"title": ("like", f"{cls.MIRROR_TITLE_PREFIX}%")},
            ),
            (
                _SC_DOCTYPE,
                {"slug": ("like", f"{cls.SC_SLUG_PREFIX}%")},
            ),
        ):
            for name in frappe.get_all(dt, filters=filters, pluck="name"):
                try:
                    frappe.delete_doc(
                        dt, name, force=True, ignore_permissions=True,
                    )
                except Exception:
                    pass
        # Wipe any Mirrors created by the patch under test that point
        # at our synthetic IGs (the migration derives the title from
        # the SC title; the prefix wipe above covers SC-derived ones
        # but a pre-existing mirror would have a different title).
        for name in frappe.get_all(
            _MIRROR_DOCTYPE,
            filters={"root_item_group": ("like", f"{cls.PREFIX}%")},
            pluck="name",
        ):
            try:
                frappe.delete_doc(
                    _MIRROR_DOCTYPE, name, force=True, ignore_permissions=True,
                )
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

    def setUp(self):
        # Each test starts from a clean SC/Mirror slate but reuses the
        # shared IG tree from setUpClass — IG churn between tests
        # invalidates nested-set indexes and is slow.
        for dt, filters in (
            (
                _MIRROR_DOCTYPE,
                {"title": ("like", f"{self.MIRROR_TITLE_PREFIX}%")},
            ),
            (
                _SC_DOCTYPE,
                {"slug": ("like", f"{self.SC_SLUG_PREFIX}%")},
            ),
        ):
            for name in frappe.get_all(dt, filters=filters, pluck="name"):
                try:
                    frappe.delete_doc(
                        dt, name, force=True, ignore_permissions=True,
                    )
                except Exception:
                    pass
        # Also wipe Mirrors derived from this test's IGs (e.g. when
        # we explicitly seed a pre-existing Mirror in one test).
        for name in frappe.get_all(
            _MIRROR_DOCTYPE,
            filters={"root_item_group": self.branch},
            pluck="name",
        ):
            try:
                frappe.delete_doc(
                    _MIRROR_DOCTYPE, name, force=True, ignore_permissions=True,
                )
            except Exception:
                pass

    def _insert_sc(
        self,
        *,
        slug: str,
        title: str,
        rules: list[dict],
        targets: list[dict],
    ) -> str:
        doc = frappe.get_doc({
            "doctype": _SC_DOCTYPE,
            "title": title,
            "slug": slug,
            "is_active": 1,
            "rule_combinator": "AND",
            "rules": rules,
            "targets": targets,
        })
        doc.flags.ignore_links = True
        doc.flags.ignore_permissions = True
        doc.insert()
        return doc.name

    # ------------------------------------------------------------------
    # Happy path
    # ------------------------------------------------------------------

    def test_happy_path_creates_mirror_and_disables_target(self):
        """One umbrella SC with one IG rule + one Shopware target →
        one Mirror created, target disabled, breadcrumb stamped."""
        sc_name = self._insert_sc(
            slug=f"{self.SC_SLUG_PREFIX}-happy",
            title=f"{self.MIRROR_TITLE_PREFIX}-happy",
            rules=[{
                "rule_type": "Item Group",
                "operator": "descends_from",
                "value": self.branch,
            }],
            targets=[{
                "backend": "Shopware",
                "sales_channel": self.CHANNEL_A,
                "enabled": 1,
                "visibility": "All (30)",
            }],
        )

        migrate_execute()

        mirrors = frappe.get_all(
            _MIRROR_DOCTYPE,
            filters={
                "backend": "Shopware",
                "target_sales_channel": self.CHANNEL_A,
                "root_item_group": self.branch,
            },
            fields=["name", "title", "is_active", "orphan_policy"],
        )
        self.assertEqual(len(mirrors), 1)
        self.assertEqual(int(mirrors[0].is_active), 1)
        self.assertEqual(mirrors[0].orphan_policy, "keep")
        self.assertTrue(mirrors[0].title.endswith(" — Catalog Mirror"))

        # SC target should be disabled with a breadcrumb on last_error.
        sc = frappe.get_doc(_SC_DOCTYPE, sc_name)
        self.assertEqual(len(sc.targets), 1)
        self.assertEqual(int(sc.targets[0].enabled or 0), 0)
        self.assertIn("Catalog Mirror", sc.targets[0].last_error or "")
        self.assertIn(mirrors[0].name, sc.targets[0].last_error or "")

    # ------------------------------------------------------------------
    # Idempotency
    # ------------------------------------------------------------------

    def test_idempotent_second_run_creates_no_duplicate(self):
        """Re-running the patch must not produce a second Mirror."""
        self._insert_sc(
            slug=f"{self.SC_SLUG_PREFIX}-idem",
            title=f"{self.MIRROR_TITLE_PREFIX}-idem",
            rules=[{
                "rule_type": "Item Group",
                "operator": "descends_from",
                "value": self.branch,
            }],
            targets=[{
                "backend": "Shopware",
                "sales_channel": self.CHANNEL_B,
                "enabled": 1,
                "visibility": "All (30)",
            }],
        )

        migrate_execute()
        first_count = frappe.db.count(
            _MIRROR_DOCTYPE,
            filters={
                "backend": "Shopware",
                "target_sales_channel": self.CHANNEL_B,
                "root_item_group": self.branch,
            },
        )
        self.assertEqual(first_count, 1)

        migrate_execute()
        second_count = frappe.db.count(
            _MIRROR_DOCTYPE,
            filters={
                "backend": "Shopware",
                "target_sales_channel": self.CHANNEL_B,
                "root_item_group": self.branch,
            },
        )
        self.assertEqual(second_count, 1)

    # ------------------------------------------------------------------
    # Skip cases
    # ------------------------------------------------------------------

    def test_multi_rule_sc_is_skipped(self):
        """SC with more than one rule must be left untouched."""
        sc_name = self._insert_sc(
            slug=f"{self.SC_SLUG_PREFIX}-multi",
            title=f"{self.MIRROR_TITLE_PREFIX}-multi",
            rules=[
                {
                    "rule_type": "Item Group",
                    "operator": "descends_from",
                    "value": self.branch,
                },
                {
                    "rule_type": "Manufacturer",
                    "operator": "in",
                    "value": "Demo Manufacturer",
                },
            ],
            targets=[{
                "backend": "Shopware",
                "sales_channel": self.CHANNEL_A,
                "enabled": 1,
                "visibility": "All (30)",
            }],
        )

        migrate_execute()

        mirrors = frappe.get_all(
            _MIRROR_DOCTYPE,
            filters={
                "backend": "Shopware",
                "target_sales_channel": self.CHANNEL_A,
                "root_item_group": self.branch,
            },
        )
        self.assertEqual(len(mirrors), 0)

        # SC target stays enabled — nothing replaced it.
        sc = frappe.get_doc(_SC_DOCTYPE, sc_name)
        self.assertEqual(int(sc.targets[0].enabled or 0), 1)

    def test_sc_with_no_rules_is_skipped(self):
        """SC with zero rules is also unmigratable — must be skipped."""
        sc_name = self._insert_sc(
            slug=f"{self.SC_SLUG_PREFIX}-norules",
            title=f"{self.MIRROR_TITLE_PREFIX}-norules",
            rules=[],
            targets=[{
                "backend": "Shopware",
                "sales_channel": self.CHANNEL_A,
                "enabled": 1,
                "visibility": "All (30)",
            }],
        )

        migrate_execute()

        sc = frappe.get_doc(_SC_DOCTYPE, sc_name)
        self.assertEqual(int(sc.targets[0].enabled or 0), 1)
        self.assertEqual(
            frappe.db.count(
                _MIRROR_DOCTYPE,
                filters={"target_sales_channel": self.CHANNEL_A,
                         "root_item_group": self.branch},
            ),
            0,
        )

    def test_sc_with_no_shopware_target_is_skipped(self):
        """Medusa-only SCs are out of scope — patch must skip cleanly."""
        sc_name = self._insert_sc(
            slug=f"{self.SC_SLUG_PREFIX}-medusa-only",
            title=f"{self.MIRROR_TITLE_PREFIX}-medusa-only",
            rules=[{
                "rule_type": "Item Group",
                "operator": "descends_from",
                "value": self.branch,
            }],
            targets=[{
                "backend": "Medusa",
                "sales_channel": "medusa-channel-placeholder",
                "enabled": 1,
                "visibility": "All (30)",
            }],
        )

        migrate_execute()

        # No Mirror was created (we don't migrate Medusa here).
        sc = frappe.get_doc(_SC_DOCTYPE, sc_name)
        self.assertEqual(int(sc.targets[0].enabled or 0), 1)

    def test_already_migrated_sc_skips_existing(self):
        """When a Mirror already exists for (backend, channel, root_ig)
        the patch must not create a second one and must still disable
        a lingering enabled SC target."""
        # Pre-seed the Mirror that a previous patch run would have
        # produced.
        pre = frappe.get_doc({
            "doctype": _MIRROR_DOCTYPE,
            "title": f"{self.MIRROR_TITLE_PREFIX}-preexisting — Catalog Mirror",
            "backend": "Shopware",
            "is_active": 1,
            "target_sales_channel": self.CHANNEL_C,
            "root_item_group": self.branch,
            "orphan_policy": "keep",
            "name_template": "{{ item_group.item_group_name }}",
        })
        pre.flags.ignore_links = True
        pre.flags.ignore_permissions = True
        pre.insert()

        sc_name = self._insert_sc(
            slug=f"{self.SC_SLUG_PREFIX}-preexisting",
            title=f"{self.MIRROR_TITLE_PREFIX}-preexisting",
            rules=[{
                "rule_type": "Item Group",
                "operator": "descends_from",
                "value": self.branch,
            }],
            targets=[{
                "backend": "Shopware",
                "sales_channel": self.CHANNEL_C,
                "enabled": 1,
                "visibility": "All (30)",
            }],
        )

        migrate_execute()

        # Still only one Mirror for that triple.
        self.assertEqual(
            frappe.db.count(
                _MIRROR_DOCTYPE,
                filters={
                    "backend": "Shopware",
                    "target_sales_channel": self.CHANNEL_C,
                    "root_item_group": self.branch,
                },
            ),
            1,
        )

        # SC target ended up disabled, with the existing Mirror name
        # in the breadcrumb (not a freshly-created one).
        sc = frappe.get_doc(_SC_DOCTYPE, sc_name)
        self.assertEqual(int(sc.targets[0].enabled or 0), 0)
        self.assertIn(pre.name, sc.targets[0].last_error or "")
