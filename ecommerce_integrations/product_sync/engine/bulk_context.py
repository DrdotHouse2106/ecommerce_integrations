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
    # AI-description custom fields. Same ``getattr`` defensiveness as
    # the SEO slots — sites without ai_description installed simply
    # never see these populated, and the canonical builder hashes the
    # empty default. Critical for delta-sync parity: the apply phase
    # reads these off the full ``frappe.get_doc("Item", …)`` so the
    # differ-side ItemSnapshot must carry them too, otherwise the two
    # hashes never match and every item with any AI content looks
    # permanently drifted.
    ai_short_description: str | None = None
    ai_benefits: str | None = None
    ai_seo_description: str | None = None
    ai_long_description: str | None = None
    ai_seo_title: str | None = None
    youtube_video_url: str | None = None
    ean: str | None = None
    delivery_time: str | None = None
    # Pre-loaded child lists. The canonical reads ``item.attributes``
    # (Item Variant Attribute) and ``item.taxes`` (Item Tax) via plain
    # ``getattr``; both must be populated or the differ-side canonical
    # diverges from the apply-side canonical and every Item with any
    # variant attribute or tax template looks permanently drifted.
    attributes: list[Any] = field(default_factory=list)
    barcodes: list[Any] = field(default_factory=list)
    taxes: list[Any] = field(default_factory=list)
    # Operator-configured dynamic field values keyed by Item fieldname.
    # Populated by ``_load_dynamic_fields`` for the keys listed on
    # Shopware Setting.item_custom_field_mappings. ``__getattr__``
    # below makes ``getattr(snap, 'include_in_idealo_feed')`` work
    # transparently so the canonical's plain ``getattr`` call is
    # backend-symmetric with the apply-side full-doc read.
    dynamic_field_values: dict[str, Any] = field(default_factory=dict)

    def __getattr__(self, name: str) -> Any:
        """Fallback for dataclass-undeclared attributes — looks
        them up in ``dynamic_field_values`` so the canonical's
        ``getattr(item, mapping['item_field'], None)`` returns the
        mapped value rather than ``None`` for an item loaded via
        ctx. Raises ``AttributeError`` for genuinely unknown fields
        so ``getattr(item, 'x', default)`` still falls back to the
        default."""
        try:
            return self.dynamic_field_values[name]
        except KeyError:
            raise AttributeError(name)

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


@dataclass
class TaxRow:
    item_tax_template: str


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
        self._load_attributes(codes)
        self._load_taxes(codes)
        self._load_dynamic_fields(codes)

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
            "ai_short_description", "ai_benefits",
            "ai_seo_description", "ai_long_description", "ai_seo_title",
            "youtube_video_url", "ean", "delivery_time",
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
                ai_short_description=r.get("ai_short_description"),
                ai_benefits=r.get("ai_benefits"),
                ai_seo_description=r.get("ai_seo_description"),
                ai_long_description=r.get("ai_long_description"),
                ai_seo_title=r.get("ai_seo_title"),
                youtube_video_url=r.get("youtube_video_url"),
                ean=r.get("ean"),
                delivery_time=r.get("delivery_time"),
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

    def _load_attributes(self, item_codes: list[str]) -> None:
        """Pre-load Item Variant Attribute rows. The canonical hashes
        these into ``properties.attributes`` — without them, items
        carrying any variant attribute (colour, size, lock-type, …)
        look permanently drifted to the differ."""
        rows = frappe.db.sql(
            """SELECT parent, attribute, attribute_value
               FROM `tabItem Variant Attribute`
               WHERE parent IN %(codes)s
               ORDER BY parent, idx""",
            {"codes": tuple(item_codes) or ("__none__",)},
            as_dict=True,
        )
        for r in rows:
            snap = self._items.get(r["parent"])
            if snap is None:
                continue
            snap.attributes.append(
                AttributeRow(
                    attribute=r["attribute"] or "",
                    attribute_value=r["attribute_value"] or "",
                ),
            )

    def _load_dynamic_fields(self, item_codes: list[str]) -> None:
        """Pre-load operator-configured Item field values.

        Reads ``Shopware Setting.item_custom_field_mappings``, picks
        the ``item_field`` column names that actually exist on
        ``tabItem`` (so a stale mapping after a Custom Field rename
        doesn't crash the query), and fetches the values in one
        bulk SELECT. Values land in each snapshot's
        ``dynamic_field_values`` dict so the canonical's
        ``getattr(item, mapping['item_field'])`` reads them via the
        snapshot's ``__getattr__`` fallback. The whole loader is a
        silent no-op on sites without the doctype installed
        (Medusa-only) or with no mappings configured."""
        try:
            settings = frappe.get_cached_doc("Shopware Setting")
            mappings = getattr(settings, "item_custom_field_mappings", None) or []
        except Exception:  # noqa: BLE001
            return
        wanted_fields: list[str] = []
        meta = frappe.get_meta("Item")
        present = {f.fieldname for f in meta.get("fields") or []}
        seen: set[str] = set()
        for row in mappings:
            f = (getattr(row, "item_field", "") or "").strip()
            if f and f in present and f not in seen:
                wanted_fields.append(f)
                seen.add(f)
        if not wanted_fields:
            return
        cols = ", ".join([f"`{f}`" for f in (["name"] + wanted_fields)])
        rows = frappe.db.sql(
            f"""SELECT {cols} FROM `tabItem`
               WHERE name IN %(codes)s""",
            {"codes": tuple(item_codes) or ("__none__",)},
            as_dict=True,
        )
        for r in rows:
            snap = self._items.get(r["name"])
            if snap is None:
                continue
            for f in wanted_fields:
                snap.dynamic_field_values[f] = r.get(f)

    def _load_taxes(self, item_codes: list[str]) -> None:
        """Pre-load Item Tax template assignments. The canonical's
        tax-rate derivation in ``_max_tax_rate_from_item`` walks
        ``item.taxes`` for an ``item_tax_template`` — without it the
        ctx-path computes 0 % VAT while the apply-path computes the
        real rate (19 % DE default), and every item with a tax
        template shows drift on the ``taxes`` section."""
        rows = frappe.db.sql(
            """SELECT parent, item_tax_template
               FROM `tabItem Tax`
               WHERE parent IN %(codes)s
                 AND item_tax_template IS NOT NULL
                 AND item_tax_template != ''
               ORDER BY parent, idx""",
            {"codes": tuple(item_codes) or ("__none__",)},
            as_dict=True,
        )
        for r in rows:
            snap = self._items.get(r["parent"])
            if snap is None:
                continue
            snap.taxes.append(
                TaxRow(item_tax_template=r["item_tax_template"] or ""),
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
