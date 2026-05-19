"""Pre-loaded in-memory snapshot of Items + dependent data.

The canonical-payload builder used to issue 4 queries per Item
(``frappe.get_doc("Item",…)``, ``tabBin`` sum, ``tabItem Price`` lookup,
``tabFile`` list). At 30k items that's 120k round-trips against the
DB — roughly 6 minutes wall-clock.

``BulkContext`` does the equivalent work in **5 bulk queries**:

1. ``tabItem`` rows for every code in scope (all canonical-relevant
   fields).
2. ``tabBin`` ``SUM(actual_qty)`` GROUP BY ``item_code``.
3. ``tabItem Price`` filtered to the price lists this Sync touches.
4. ``tabFile`` attached to any of the in-scope items.
5. ``tabItem Barcode`` for EAN/UPC.

After construction, ``ctx.item(item_code)`` returns a duck-typed
``ItemSnapshot`` with the same attribute names the canonical-payload
builder expects (``item_name``, ``description``, ``brand``, etc.).
Lookups for stock, prices, images, and barcodes are O(1).

Estimated speedup at 30k items: ~6 min → ~5 s.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import frappe


@dataclass
class ItemSnapshot:
    """Duck-typed Item replacement passed to canonical builders.

    Carries every attribute the canonical sections read. Anything not
    pre-loaded returns the default (None / empty list) so a canonical
    builder asking for an exotic field doesn't crash — it just hashes
    "no value", which is correct.
    """

    item_code: str
    item_name: str | None = None
    description: str | None = None
    disabled: int = 0
    has_variants: int = 0
    variant_of: str | None = None
    stock_uom: str | None = None
    standard_rate: float = 0.0
    currency: str | None = None
    image: str | None = None
    brand: str | None = None
    manufacturer: str | None = None
    item_group: str | None = None
    # Custom-field SEO slots — empty by default; canonical falls back to None.
    seo_slug: str | None = None
    slug: str | None = None
    meta_title: str | None = None
    meta_description: str | None = None
    # Pre-loaded child lists.
    attributes: list[Any] = field(default_factory=list)
    barcodes: list[Any] = field(default_factory=list)

    def as_dict(self) -> dict:
        """Used by name_template / description_template Jinja rendering."""
        return {
            "item_code": self.item_code,
            "item_name": self.item_name,
            "name": self.item_code,
            "description": self.description,
            "brand": self.brand,
            "manufacturer": self.manufacturer,
            "item_group": self.item_group,
            "stock_uom": self.stock_uom,
        }


@dataclass
class BarcodeRow:
    barcode: str
    barcode_type: str


@dataclass
class AttributeRow:
    attribute: str
    attribute_value: str


class BulkContext:
    """Pre-loaded snapshot for one Sync's full scope.

    Construct once at the top of the differ, then hand to each
    canonical-payload call. All lookups (``item``, ``stock_for``,
    ``price_for``, ``images_for``) are O(1) dict reads.
    """

    def __init__(self, item_codes: list[str], price_lists: list[str]):
        self._items: dict[str, ItemSnapshot] = {}
        self._stock: dict[str, float] = {}
        self._prices: dict[tuple[str, str], float] = {}
        self._images: dict[str, list[str]] = {}
        if not item_codes:
            return
        codes = list({c for c in item_codes if c})
        self._load_items(codes)
        self._load_stock(codes)
        self._load_prices(codes, list({p for p in price_lists if p}))
        self._load_images(codes)
        self._load_barcodes(codes)

    # ─── Loaders ────────────────────────────────────────────────────

    def _load_items(self, item_codes: list[str]) -> None:
        """One ``tabItem`` query covering every canonical-relevant column.

        Custom fields (``seo_slug``, ``meta_title``, …) are read defensively
        — they may not exist on every site. ``frappe.get_all`` silently
        drops unknown fields, so we probe the meta once and only request
        those that exist.
        """
        meta = frappe.get_meta("Item")
        present = {f.fieldname for f in meta.get("fields") or []}
        # ``name`` is always present (the docname column). Everything
        # else is added only when the site actually has the column —
        # ERPNext ships a leaner Item by default and operators wire
        # ``brand`` / ``manufacturer`` / SEO fields in via Custom Field.
        wanted = ["name"]
        for cf in (
            "item_name", "description", "disabled", "has_variants",
            "variant_of", "stock_uom", "standard_rate", "image",
            "item_group", "brand", "manufacturer", "currency",
            "seo_slug", "slug", "meta_title", "meta_description",
        ):
            if cf in present:
                wanted.append(cf)
        rows = frappe.get_all(
            "Item",
            filters={"name": ("in", item_codes)},
            fields=wanted,
            limit=0,
        )
        for r in rows:
            snap = ItemSnapshot(
                item_code=r["name"],
                item_name=r.get("item_name"),
                description=r.get("description"),
                disabled=int(r.get("disabled") or 0),
                has_variants=int(r.get("has_variants") or 0),
                variant_of=r.get("variant_of"),
                stock_uom=r.get("stock_uom"),
                standard_rate=float(r.get("standard_rate") or 0),
                currency=r.get("currency"),
                image=r.get("image"),
                brand=r.get("brand"),
                manufacturer=r.get("manufacturer"),
                item_group=r.get("item_group"),
                seo_slug=r.get("seo_slug"),
                slug=r.get("slug"),
                meta_title=r.get("meta_title"),
                meta_description=r.get("meta_description"),
            )
            self._items[snap.item_code] = snap

    def _load_stock(self, item_codes: list[str]) -> None:
        rows = frappe.db.sql(
            """SELECT item_code, COALESCE(SUM(actual_qty), 0) AS qty
               FROM `tabBin`
               WHERE item_code IN %(codes)s
               GROUP BY item_code""",
            {"codes": tuple(item_codes) or ("__none__",)},
            as_dict=True,
        )
        for r in rows:
            self._stock[r["item_code"]] = float(r["qty"] or 0)

    def _load_prices(self, item_codes: list[str], price_lists: list[str]) -> None:
        if not price_lists:
            return
        rows = frappe.db.sql(
            """SELECT item_code, price_list, price_list_rate
               FROM `tabItem Price` ip1
               WHERE item_code IN %(codes)s
                 AND price_list IN %(lists)s
                 AND modified = (
                     SELECT MAX(modified) FROM `tabItem Price` ip2
                     WHERE ip2.item_code = ip1.item_code
                       AND ip2.price_list = ip1.price_list
                 )""",
            {
                "codes": tuple(item_codes) or ("__none__",),
                "lists": tuple(price_lists) or ("__none__",),
            },
            as_dict=True,
        )
        for r in rows:
            self._prices[(r["item_code"], r["price_list"])] = float(r["price_list_rate"] or 0)

    def _load_images(self, item_codes: list[str]) -> None:
        rows = frappe.db.sql(
            """SELECT attached_to_name, file_url
               FROM `tabFile`
               WHERE attached_to_doctype = 'Item'
                 AND attached_to_name IN %(codes)s
                 AND is_private = 0
                 AND is_folder = 0""",
            {"codes": tuple(item_codes) or ("__none__",)},
            as_dict=True,
        )
        for r in rows:
            url = (r["file_url"] or "").strip()
            if not url:
                continue
            self._images.setdefault(r["attached_to_name"], []).append(url)

    def _load_barcodes(self, item_codes: list[str]) -> None:
        rows = frappe.db.sql(
            """SELECT parent, barcode, barcode_type
               FROM `tabItem Barcode`
               WHERE parent IN %(codes)s""",
            {"codes": tuple(item_codes) or ("__none__",)},
            as_dict=True,
        )
        for r in rows:
            snap = self._items.get(r["parent"])
            if snap is None:
                continue
            snap.barcodes.append(
                BarcodeRow(
                    barcode=r["barcode"] or "",
                    barcode_type=r["barcode_type"] or "",
                ),
            )

    # ─── Lookup API used by canonical.py ────────────────────────────

    def item(self, item_code: str) -> ItemSnapshot | None:
        return self._items.get(item_code)

    def stock_for(self, item_code: str) -> float:
        return self._stock.get(item_code, 0.0)

    def price_for(self, item_code: str, price_list: str) -> float | None:
        if not price_list:
            return None
        return self._prices.get((item_code, price_list))

    def images_for(self, item_code: str) -> list[str]:
        return self._images.get(item_code, [])
