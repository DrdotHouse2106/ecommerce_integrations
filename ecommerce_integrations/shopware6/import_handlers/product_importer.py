"""One-time backfill: pull the full Shopware product catalogue (parents,
variants, prices, stock, images, properties, brand, delivery time,
closeout/SEO, dimensions) into ERPNext as Items.

Companion to ``category_importer``/``product_category_linker`` — run
those first so ``Item Group.shopware_category_id`` mappings exist;
this module degrades gracefully (falls back to the shared "Shopware"
root Item Group) if run standalone, it just won't be able to resolve
real categories yet.

Matching is by SKU (Shopware ``productNumber`` == ERPNext ``item_code``)
first, regardless of whether the existing Item is a variant — a
pre-existing WeClapp-imported variant with a matching SKU is linked,
never duplicated. A match only ever adds an ``Ecommerce Item`` link and
non-destructive ``additional_item_groups`` rows; an existing Item's own
``item_name``/``item_group``/``variant_of`` are never rewritten. New
Items get their primary ``item_group`` from the first Shopware category
that resolves to an already-imported Item Group; the rest become
``additional_item_groups`` rows (same append-only helper
``product_category_linker`` already uses).

Closeout (``abverkauf``) and SEO (``metatitel``/``metabeschreibung``/
``metakeywords``) are pre-existing, operator-installed (WeClapp-origin)
Item fields, written defensively via ``frappe.get_meta(...).has_field``
guards — same pattern the export direction already reads them with
(``export/product_mapper.py``). Same for dimensions
(``item_height``/``item_width``/``item_length``, mm→cm) — none of these
fields are defined by this plugin, so nothing here creates them if a
site doesn't have them.

Deliberately excluded from this pass:
- Cross-selling (both directions) — no ERPNext-side data model decided
  yet.
- Tiered / quantity / customer-group pricing — Shopware's rule-based
  channel pricing isn't visible on a plain product fetch, and mapping
  it would mean auto-generating ERPNext Pricing Rules (app-wide
  Sales Order blast radius) — a separate feature on its own.
- ``Item.shopware_channel_overrides`` — that field is a manual,
  operator-controlled visibility override (per the resolver in
  ``product_sync/resolver.py``); pulling Shopware's live visibility
  into it would silently corrupt its veto/inject semantics. Never
  written here.
- Recurring/incremental sync — this is a one-time button, not wired
  into the 15-minute ``Ecommerce Pull Sync`` cron.

Gallery images are stored as ``File`` docs pointing at Shopware's own
CDN URL (``file_url``, no byte download) — the same convention
``category_importer._maybe_set_image`` already uses for category
images, and exactly what ``product_sync/engine/canonical.py``'s
``_canonical_images`` already expects to read back on the export side.
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe import _
from frappe.utils import cstr, flt, now

from ecommerce_integrations.ecommerce_integrations.doctype.ecommerce_item import ecommerce_item
from ecommerce_integrations.shopware6.connection import get_shopware_client
from ecommerce_integrations.shopware6.constants import MODULE_NAME, SETTING_DOCTYPE
from ecommerce_integrations.shopware6.import_handlers.category_importer import _ensure_root_item_group
from ecommerce_integrations.shopware6.import_handlers.product_category_linker import (
    _add_additional_item_groups,
    _build_category_item_group_map,
)
from ecommerce_integrations.shopware6.import_handlers.property_importer import PropertyImporter
from ecommerce_integrations.shopware6.import_handlers.stock_importer import StockImporter
from ecommerce_integrations.shopware6.utils import create_shopware_log, update_shopware_log

_PAGE_SIZE = 100


@frappe.whitelist()
def import_products_from_shopware() -> dict[str, Any]:
    """Entry point for the "Produkte aus Shopware importieren" button.

    Runs as a background job — same reasoning as
    ``category_importer.import_categories_from_shopware``: a full
    paginated catalogue crawl (association-rich per row, one or more
    ``Item.save()`` calls per product) reliably exceeds the web
    worker's request timeout on anything but a tiny catalogue.
    """
    frappe.only_for("System Manager")

    setting = frappe.get_doc(SETTING_DOCTYPE)
    if not setting.is_enabled():
        frappe.throw(_("Bitte zuerst die Shopware-Integration aktivieren"))

    log = create_shopware_log(
        status="Queued",
        method="product_import",
        message=_("Produkt-Import wurde eingereiht..."),
        make_new=True,
    )

    frappe.enqueue(
        _run_product_import,
        queue="long",
        timeout=3600 * 4,
        job_name=f"shopware6_product_import_{log.name}",
        request_id=log.name,
        enqueue_after_commit=True,
    )

    return {"queued": True, "log": log.name}


def _run_product_import(request_id: str) -> None:
    stats: dict[str, Any] = {
        "products_seen": 0,
        "created": 0,
        "variants_created": 0,
        "matched": 0,
        "category_links_added": 0,
        "prices_set": 0,
        "images_set": 0,
        "stock_adjusted": 0,
        "descriptions_set": 0,
        "variant_of_mismatches": 0,
        "errors": [],
    }

    # Every Item.save() below fires the "Item" on_update hook, which
    # would otherwise queue an outbound push of the very data just
    # pulled from Shopware straight back — redundant, and a guaranteed
    # 403 under read-only integration permissions. Suppress for the
    # whole run, same as category_importer/product_category_linker.
    previous_skip_flag = getattr(frappe.flags, "skip_shopware_sync", False)
    frappe.flags.skip_shopware_sync = True
    try:
        setting = frappe.get_doc(SETTING_DOCTYPE)
        warehouse = setting.warehouse

        category_to_item_group = _build_category_item_group_map()
        if not category_to_item_group:
            stats["errors"].append(
                'Keine importierten Kategorien gefunden — Artikel werden ohne echte '
                'Kategorie-Zuordnung importiert. Für vollständige Zuordnung zuerst '
                '"Kategorien aus Shopware importieren" ausführen.'
            )

        property_importer = PropertyImporter()
        stock_importer = StockImporter()
        client = get_shopware_client()

        # Stock import is opt-in — same setting StockImporter's own
        # (previously dead, since the field didn't exist yet)
        # is_import_enabled() gate already reads. Reuse it rather than
        # a second flag, so the reactive per-item stock path and this
        # bulk import agree on the same switch.
        if not stock_importer.is_import_enabled():
            warehouse = None
        elif warehouse and frappe.db.get_value("Warehouse", warehouse, "is_group"):
            # ERPNext refuses any Stock Ledger Entry against a group
            # (organisational) warehouse node — every single stock
            # adjustment would otherwise fail with the same "Group
            # node warehouse is not allowed" error. Check once, skip
            # stock for the whole run with one clear message instead
            # of flooding the log with a per-item stack trace.
            stats["errors"].append(
                f'Lager "{warehouse}" in den Shopware-Einstellungen ist ein Gruppenknoten '
                '— Lagerbuchungen sind dort nicht erlaubt. Bitte in den Shopware-'
                "Einstellungen ein echtes (Blatt-)Lager auswählen. Lagerbestand wurde "
                "für diesen Lauf übersprungen, alles andere lief normal weiter."
            )
            warehouse = None

        page = 1
        while True:
            response = client.request_post("search/product", _build_criteria(page))
            products = response.data or []
            if not products:
                break

            for product_data in products:
                _import_product(
                    product_data, category_to_item_group, setting, warehouse,
                    property_importer, stock_importer, stats,
                )

            # Commit per page rather than only at the end — a job that
            # dies partway (worker restart, OOM) keeps whatever it
            # already finished instead of losing it to a rollback.
            frappe.db.commit()

            if len(products) < _PAGE_SIZE:
                break
            page += 1

        update_shopware_log(
            request_id,
            status="Success" if not stats["errors"] else "Error",
            message=_(
                "Produkte gesehen: {0}, Erstellt: {1}, Varianten erstellt: {2}, "
                "Zugeordnet (bestehender Artikel per SKU): {3}, Kategorie-Zuordnungen "
                "ergänzt: {4}, Preise gesetzt: {5}, Bilder gesetzt: {6}, "
                "Beschreibungen übernommen: {7}, Lagerbestand angepasst: {8}, "
                "Varianten-Zuordnungen ohne passende ERPNext-Vorlage (nur "
                "verknüpft, nichts verschoben): {9}"
            ).format(
                stats["products_seen"], stats["created"], stats["variants_created"],
                stats["matched"], stats["category_links_added"], stats["prices_set"],
                stats["images_set"], stats["descriptions_set"], stats["stock_adjusted"],
                stats["variant_of_mismatches"],
            ),
            exception="\n".join(stats["errors"]) if stats["errors"] else None,
        )
    except Exception as e:
        update_shopware_log(request_id, status="Error", exception=str(e))
        raise
    finally:
        frappe.flags.skip_shopware_sync = previous_skip_flag


def _build_criteria(page: int) -> dict[str, Any]:
    return {
        "page": page,
        "limit": _PAGE_SIZE,
        "filter": [{"type": "equals", "field": "parentId", "value": None}],
        "associations": {
            "tax": {},
            "media": {"associations": {"media": {}}},
            "cover": {"associations": {"media": {}}},
            "properties": {"associations": {"group": {}}},
            "options": {"associations": {"group": {}}},
            "prices": {},
            "categories": {},
            "manufacturer": {},
            "deliveryTime": {},
            "unit": {},
            # NOT an association — customFields is a plain JSON column on
            # the product entity, always present in the response by
            # default. Requesting it as an association 500s with
            # FRAMEWORK__ASSOCIATION_NOT_FOUND (confirmed against a real
            # Shopware instance). PropertyImporter's per-product GET gets
            # away with `associations[customFields]={}` because that's a
            # different (query-string) request shape, not this criteria.
            "children": {
                "associations": {
                    "prices": {},
                    "options": {"associations": {"group": {}}},
                    "tax": {},
                    "media": {"associations": {"media": {}}},
                    "cover": {"associations": {"media": {}}},
                    "unit": {},
                    "manufacturer": {},
                    "deliveryTime": {},
                },
            },
        },
    }


def _import_product(
    product_data: dict[str, Any],
    category_to_item_group: dict[str, str],
    setting,
    warehouse: str | None,
    property_importer: PropertyImporter,
    stock_importer: StockImporter,
    stats: dict[str, Any],
) -> None:
    stats["products_seen"] += 1
    identifier = product_data.get("productNumber") or product_data.get("id")
    try:
        item_groups = _resolve_categories(product_data, category_to_item_group)
        primary_group, extra_groups = _split_primary_and_extra(item_groups)

        children = product_data.get("children") or []
        if children:
            attributes = _build_variant_attributes(product_data, property_importer)
            template_code, template_matched = _import_template(product_data, primary_group, attributes, stats)
            if not template_code:
                return
            _link_categories(template_code, item_groups if template_matched else extra_groups, stats)
            # Parent/template products carry no sellable price or stock
            # in Shopware — only their variants do.
            _finish_item(
                template_code, product_data, setting, warehouse,
                property_importer, stock_importer, stats, include_price_stock=False,
            )

            for child in children:
                # Per-child isolation: one broken variant (missing
                # option value, validation error) must not abort the
                # remaining siblings of the same template.
                try:
                    child = _merge_inherited_fields(child, product_data)
                    child_code, child_matched = _import_variant(child, template_code, primary_group, attributes, stats)
                    if not child_code:
                        continue
                    _link_categories(child_code, item_groups if child_matched else extra_groups, stats)
                    _finish_item(
                        child_code, child, setting, warehouse,
                        property_importer, stock_importer, stats, include_price_stock=True,
                    )
                except Exception as e:
                    stats["errors"].append(f"{child.get('productNumber') or child.get('id')}: {e}")
                    frappe.log_error(
                        title="Shopware product import: variant failed",
                        message=frappe.get_traceback(),
                    )
        else:
            item_code, matched = _import_simple_product(product_data, primary_group, stats)
            if not item_code:
                return
            _link_categories(item_code, item_groups if matched else extra_groups, stats)
            _finish_item(
                item_code, product_data, setting, warehouse,
                property_importer, stock_importer, stats, include_price_stock=True,
            )
    except Exception as e:
        stats["errors"].append(f"{identifier}: {e}")
        frappe.log_error(title="Shopware product import failed", message=frappe.get_traceback())


def _resolve_categories(product_data: dict[str, Any], category_to_item_group: dict[str, str]) -> list[str]:
    resolved: list[str] = []
    seen: set[str] = set()
    for cat in product_data.get("categories") or []:
        ig = category_to_item_group.get(cat.get("id"))
        if ig and ig not in seen:
            resolved.append(ig)
            seen.add(ig)
    return resolved


def _split_primary_and_extra(item_groups: list[str]) -> tuple[str, list[str]]:
    if not item_groups:
        # No resolvable Shopware category (none imported yet, or none
        # matched) — fall back to the same shared "Shopware" root
        # category_importer creates, rather than failing outright.
        return _ensure_root_item_group("Shopware"), []
    return item_groups[0], item_groups[1:]


def _link_categories(item_code: str, item_groups: list[str], stats: dict[str, Any]) -> None:
    """Non-destructive: never touches item_group, only appends missing
    additional_item_groups rows (reuses product_category_linker's
    already-established helper).
    """
    if not item_groups:
        return
    added = _add_additional_item_groups(item_code, set(item_groups))
    if added:
        stats["category_links_added"] += added


def _resolve_description(data: dict[str, Any], fallback: str) -> str:
    """Shopware's ``description`` is a translatable field — like ``name``,
    it can arrive either as a top-level value or nested under
    ``translated`` depending on which language context the API resolved
    against. Fall back to the item name only if genuinely absent.
    """
    return (
        data.get("description")
        or (data.get("translated") or {}).get("description")
        or fallback
    )


def _import_simple_product(
    product_data: dict[str, Any], primary_group: str, stats: dict[str, Any],
) -> tuple[str | None, bool]:
    sku = product_data.get("productNumber") or cstr(product_data.get("id", ""))[:20]
    name = (
        product_data.get("name")
        or (product_data.get("translated") or {}).get("name")
        or sku
    ).strip()
    description = _resolve_description(product_data, name)
    uom = _ensure_uom((product_data.get("unit") or {}).get("name"))

    item_dict = {
        "item_code": sku,
        "item_name": name[:140],
        "description": description,
        "item_group": primary_group,
        "has_variants": 0,
        "stock_uom": uom,
        "is_stock_item": 1,
        "disabled": 0 if product_data.get("active", True) else 1,
    }
    return _resolve_or_create_item(item_dict, product_data["id"], product_data.get("id"), None, 0, stats)


def _import_template(
    product_data: dict[str, Any], primary_group: str, attributes: list[dict], stats: dict[str, Any],
) -> tuple[str | None, bool]:
    sku = product_data.get("productNumber") or cstr(product_data.get("id", ""))[:20]
    name = (
        product_data.get("name")
        or (product_data.get("translated") or {}).get("name")
        or sku
    ).strip()
    uom = _ensure_uom((product_data.get("unit") or {}).get("name"))

    item_dict = {
        "item_code": sku,
        "item_name": name[:140],
        "description": _resolve_description(product_data, name),
        "item_group": primary_group,
        "has_variants": 1,
        "attributes": attributes,
        "stock_uom": uom,
        "is_stock_item": 1,
    }
    return _resolve_or_create_item(item_dict, product_data["id"], None, None, 1, stats)


def _import_variant(
    child: dict[str, Any], template_item_code: str, primary_group: str, attributes: list[dict], stats: dict[str, Any],
) -> tuple[str | None, bool]:
    sku = child.get("productNumber") or cstr(child.get("id", ""))[:20]
    name = (
        child.get("name")
        or (child.get("translated") or {}).get("name")
        or sku
    ).strip()

    child_options = child.get("options") or []
    variant_attributes = []
    for attr in attributes:
        attr_copy = dict(attr)
        for option in child_options:
            group_name = (option.get("group") or {}).get("name")
            if group_name == attr["attribute"]:
                attr_copy["attribute_value"] = option.get("name") or ""
                break
        variant_attributes.append(attr_copy)

    uom = _ensure_uom((child.get("unit") or {}).get("name"))
    item_dict = {
        "item_code": sku,
        "item_name": (name or f"{template_item_code}-{sku}")[:140],
        "description": _resolve_description(child, name),
        "item_group": primary_group,
        "has_variants": 0,
        "variant_of": template_item_code,
        "attributes": variant_attributes,
        "stock_uom": uom,
        "is_stock_item": 1,
        "disabled": 0 if child.get("active", True) else 1,
    }
    return _resolve_or_create_item(item_dict, child["id"], child.get("id"), template_item_code, 0, stats)


def _build_variant_attributes(product_data: dict[str, Any], property_importer: PropertyImporter) -> list[dict]:
    """Collect the (option group → values) matrix for a variant product.

    Shopware keeps the matrix on the *children*: a parent product's own
    ``options`` list is empty (the parent-side definition lives in
    ``configuratorSettings``, which the bulk fetch doesn't expand).
    Reading only the parent therefore yields zero attributes — and
    ERPNext refuses a ``has_variants`` Item with an empty attribute
    table — so the children's options are the authoritative source
    here. The parent's list is still walked defensively in case a
    setup does populate it.
    """
    groups: dict[str, list[str]] = {}

    def _collect(options: list | None) -> None:
        for option in options or []:
            group_name = ((option.get("group") or {}).get("name") or "").strip()
            value = (option.get("name") or "").strip()
            if group_name and value and value not in groups.setdefault(group_name, []):
                groups[group_name].append(value)

    _collect(product_data.get("options"))
    for child in product_data.get("children") or []:
        _collect(child.get("options"))

    attributes = []
    for group_name, values in groups.items():
        for value in values:
            # Reuses PropertyImporter's idempotent ensure-helper instead
            # of duplicating Item Attribute creation logic.
            property_importer._ensure_item_attribute(group_name, value)
        attributes.append({"attribute": group_name})
    return attributes


# Fields Shopware resolves via parent/child inheritance: a child with
# ``None`` here means "inherit from parent", not "empty". ``stock`` is
# deliberately absent — every variant carries its own real stock.
_INHERITED_CHILD_FIELDS = (
    "description", "unit", "price", "manufacturer", "deliveryTime", "tax",
    "weight", "height", "width", "length", "isCloseout",
    "metaTitle", "metaDescription", "keywords", "customFields",
    "media", "cover",
)


def _merge_inherited_fields(child: dict[str, Any], parent: dict[str, Any]) -> dict[str, Any]:
    """Resolve Shopware's field inheritance for a variant row.

    Without this, every inherited field reads as ``None``/empty on the
    child: variants land with UOM "Nos" instead of the parent's unit,
    no Item Price, no description, and — worst — ``active: None`` is
    falsy, so every inheriting variant imported as *disabled*.
    """
    merged = dict(child)
    for key in _INHERITED_CHILD_FIELDS:
        if not merged.get(key):
            merged[key] = parent.get(key)
    # ``active`` is tri-state on children: True/False are explicit,
    # None means inherit — only fill in the None case so an explicitly
    # deactivated variant stays deactivated.
    if merged.get("active") is None:
        merged["active"] = parent.get("active", True)
    return merged


def _resolve_or_create_item(
    item_dict: dict[str, Any],
    product_id: str,
    variant_id: str | None,
    variant_of: str | None,
    has_variants: int,
    stats: dict[str, Any],
) -> tuple[str | None, bool]:
    """Match an existing Item by SKU first — regardless of whether it's
    a variant — and only create a new Item when nothing with that SKU
    exists. A match never rewrites the existing Item's own fields, only
    adds the Ecommerce Item link.

    Returns (item_code, was_matched).
    """
    sku = item_dict.get("item_code")
    existing_item_code = frappe.db.get_value("Item", {"item_code": sku}) if sku else None

    if existing_item_code:
        if variant_of:
            existing_variant_of = frappe.db.get_value("Item", existing_item_code, "variant_of")
            if existing_variant_of != variant_of:
                # Expected, not exceptional, on a WeClapp-origin catalogue:
                # WeClapp doesn't use ERPNext's native has_variants/
                # variant_of at all, so Shopware modelling a product as
                # parent+children will almost always "mismatch" against
                # the flat WeClapp item structure. Track it as an
                # informational count, not a per-item error line — with
                # thousands of variants this would otherwise flood the
                # log and flip the whole run to "Error" for something
                # that isn't actually broken.
                stats["variant_of_mismatches"] = stats.get("variant_of_mismatches", 0) + 1
        if not ecommerce_item.is_synced(MODULE_NAME, product_id, variant_id=variant_id, sku=sku):
            try:
                frappe.get_doc({
                    "doctype": "Ecommerce Item",
                    "integration": MODULE_NAME,
                    "erpnext_item_code": existing_item_code,
                    "integration_item_code": product_id,
                    "has_variants": has_variants,
                    "variant_id": cstr(variant_id),
                    "variant_of": cstr(variant_of),
                    "sku": cstr(sku) if not has_variants else None,
                    "item_synced_on": now(),
                }).insert(ignore_permissions=True)
            except Exception:
                frappe.log_error(
                    title="Shopware product import: link failed",
                    message=frappe.get_traceback(),
                )
        stats["matched"] += 1
        return existing_item_code, True

    ecommerce_item.create_ecommerce_item(
        MODULE_NAME, product_id, item_dict,
        variant_id=variant_id, sku=sku, variant_of=variant_of, has_variants=has_variants,
    )
    if variant_of:
        stats["variants_created"] += 1
    else:
        stats["created"] += 1
    return item_dict.get("item_code"), False


def _finish_item(
    item_code: str,
    data: dict[str, Any],
    setting,
    warehouse: str | None,
    property_importer: PropertyImporter,
    stock_importer: StockImporter,
    stats: dict[str, Any],
    include_price_stock: bool,
) -> None:
    _apply_supplementary_fields(item_code, data, stats)

    custom_fields = data.get("customFields") or {}
    if custom_fields:
        property_importer._update_item_custom_fields(item_code, custom_fields)

    _write_images(item_code, data, stats)

    if include_price_stock:
        _write_item_prices(item_code, data, setting, stats)
        if warehouse:
            _write_stock(stock_importer, item_code, data, stats)


def _apply_supplementary_fields(item_code: str, data: dict[str, Any], stats: dict[str, Any]) -> None:
    """Brand, delivery time, closeout/SEO, weight/dimensions — all via
    hasattr/meta.has_field guards, mirroring export/product_mapper.py's
    read side exactly in reverse. None of these are Item identity
    fields, so it's safe to write them on a matched (pre-existing) Item
    too.
    """
    item = frappe.get_doc("Item", item_code)
    meta = frappe.get_meta("Item")
    changed = False

    # Synced unconditionally, same as brand/delivery_time/weight below —
    # this run's whole point is to make ERPNext the authoritative mirror
    # of Shopware's catalogue, so a WeClapp-origin placeholder (often
    # just the item name) must not block the real Shopware description
    # from landing. Only item_code/item_name/item_group/variant_of are
    # protected identity fields (see module docstring); description is
    # not one of them. No name-fallback here: when Shopware carries no
    # description we leave the ERP field alone instead of writing noise,
    # and the counter below only counts real Shopware descriptions — so
    # the run log answers "did descriptions arrive at all?" directly.
    description = _resolve_description(data, "")
    if description and (item.description or "").strip() != description.strip():
        item.description = description
        changed = True
        stats["descriptions_set"] = stats.get("descriptions_set", 0) + 1

    manufacturer = (data.get("manufacturer") or {}).get("name")
    try:
        brand = _ensure_brand(manufacturer)
    except Exception:
        # A failed Brand insert (140-char limit, duplicate under a
        # different casing, …) must not abort the whole supplementary
        # save — description/SEO/weight above and below would be lost
        # with it.
        frappe.log_error(title="Shopware product import: brand creation failed", message=frappe.get_traceback())
        brand = None
    if brand and item.brand != brand:
        item.brand = brand
        changed = True

    delivery_time = (data.get("deliveryTime") or {}).get("name")
    if delivery_time and meta.has_field("delivery_time") and item.get("delivery_time") != delivery_time:
        item.delivery_time = delivery_time
        changed = True

    restock_time = data.get("restockTime")
    if restock_time and meta.has_field("restock_time") and item.get("restock_time") != restock_time:
        item.restock_time = restock_time
        changed = True

    if meta.has_field("abverkauf"):
        closeout = bool(data.get("isCloseout"))
        if item.get("abverkauf") != closeout:
            item.abverkauf = closeout
            changed = True
    if meta.has_field("metatitel") and data.get("metaTitle"):
        item.metatitel = data["metaTitle"]
        changed = True
    if meta.has_field("metabeschreibung") and data.get("metaDescription"):
        item.metabeschreibung = data["metaDescription"]
        changed = True
    if meta.has_field("metakeywords") and data.get("keywords"):
        item.metakeywords = data["keywords"]
        changed = True

    weight = flt(data.get("weight") or 0)
    if weight and item.weight_per_unit != weight:
        item.weight_per_unit = weight
        # Literal "Kg", not _("Kg") — same reasoning as _ensure_uom:
        # this has to match tabUOM's actual document name, not a
        # translated display label.
        item.weight_uom = item.weight_uom or "Kg"
        changed = True

    for dim_field, sw_field in (("item_height", "height"), ("item_width", "width"), ("item_length", "length")):
        if meta.has_field(dim_field) and data.get(sw_field):
            cm_value = flt(data[sw_field]) / 10  # Shopware stores mm
            if item.get(dim_field) != cm_value:
                item.set(dim_field, cm_value)
                changed = True

    if changed:
        item.flags.from_integration = True
        item.save(ignore_permissions=True)


def _ensure_brand(name: str | None) -> str | None:
    if not name:
        return None
    if frappe.db.exists("Brand", name):
        return name
    frappe.get_doc({"doctype": "Brand", "brand": name}).insert(ignore_permissions=True)
    return name


def _ensure_uom(name: str | None) -> str:
    """Shopware's unit name (e.g. "Stk", "Stück (Menge)") isn't a valid
    ERPNext UOM just because it's a string — stock_uom is a Link field,
    and saving with an unknown UOM throws. Auto-create it once rather
    than collapsing every distinct Shopware unit down to "Nos".

    The fallback is the literal document name "Nos", not _("Nos") —
    _() translates to the site's display language (e.g. German
    renders the "Nos" UOM's label as "Stk"), but a Link field needs
    the actual tabUOM primary key, not a translated label. Building a
    Link value from a translated string is exactly the label-vs-value
    mistake that bit the Select-field options earlier in this project.
    """
    name = (name or "").strip()
    if not name:
        return "Nos"
    if frappe.db.exists("UOM", name):
        return name
    try:
        frappe.get_doc({"doctype": "UOM", "uom_name": name}).insert(ignore_permissions=True)
    except Exception:
        frappe.log_error(title="Shopware product import: UOM creation failed", message=frappe.get_traceback())
        return "Nos"
    return name


def _write_item_prices(item_code: str, data: dict[str, Any], setting, stats: dict[str, Any]) -> None:
    # Only the plain ``price`` array — ``prices`` (rule-based advanced
    # prices) has a different row shape ({ruleId, price: [...]}) whose
    # rows carry no top-level gross/net and would silently read as 0.
    price_rows = data.get("price") or []
    if not price_rows:
        return
    entry = price_rows[0]

    includes_tax = bool(getattr(setting, "default_price_list_includes_tax", False))
    base_rate = flt(entry.get("gross") if includes_tax else entry.get("net"))
    default_price_list = getattr(setting, "default_selling_price_list", None)
    if base_rate > 0 and default_price_list:
        _upsert_item_price(item_code, default_price_list, base_rate)
        stats["prices_set"] += 1

    list_price = entry.get("listPrice") or {}
    uvp_includes_tax = bool(getattr(setting, "list_price_includes_tax", False))
    uvp_rate = flt(list_price.get("gross") if uvp_includes_tax else list_price.get("net"))
    uvp_price_list = getattr(setting, "list_price_price_list", None)
    if uvp_rate > 0 and uvp_price_list:
        _upsert_item_price(item_code, uvp_price_list, uvp_rate)
        stats["prices_set"] += 1


def _upsert_item_price(item_code: str, price_list: str, rate: float, currency: str = "EUR") -> None:
    existing = frappe.db.get_value(
        "Item Price", {"item_code": item_code, "price_list": price_list, "selling": 1}, "name",
    )
    if existing:
        frappe.db.set_value("Item Price", existing, "price_list_rate", rate)
    else:
        frappe.get_doc({
            "doctype": "Item Price",
            "item_code": item_code,
            "price_list": price_list,
            "selling": 1,
            "currency": currency,
            "price_list_rate": rate,
        }).insert(ignore_permissions=True)


def _write_stock(stock_importer: StockImporter, item_code: str, data: dict[str, Any], stats: dict[str, Any]) -> None:
    shopware_qty = flt(data.get("stock", 0))
    current_qty = stock_importer.get_current_erpnext_stock(item_code)
    diff = shopware_qty - current_qty
    if abs(diff) < 1:
        return
    if stock_importer.create_stock_adjustment(item_code, diff, reason="Shopware Produkt-Import"):
        stats["stock_adjusted"] += 1


def _write_images(item_code: str, data: dict[str, Any], stats: dict[str, Any]) -> None:
    media_rows = sorted(data.get("media") or [], key=lambda m: m.get("position") or 0)
    if not media_rows:
        return

    cover_media_id = ((data.get("cover") or {}).get("media") or {}).get("id")

    primary_url = None
    gallery_urls: list[str] = []
    for row in media_rows:
        media = row.get("media") or {}
        url = media.get("url")
        if not url:
            continue
        media_id = media.get("id") or row.get("mediaId")
        if cover_media_id and media_id == cover_media_id and primary_url is None:
            primary_url = url
        else:
            gallery_urls.append(url)
    if primary_url is None and gallery_urls:
        primary_url = gallery_urls.pop(0)

    if primary_url:
        current = frappe.db.get_value("Item", item_code, "image")
        if current != primary_url:
            frappe.db.set_value("Item", item_code, "image", primary_url)
        stats["images_set"] += 1

    for url in gallery_urls:
        if frappe.db.exists("File", {"attached_to_doctype": "Item", "attached_to_name": item_code, "file_url": url}):
            continue
        try:
            frappe.get_doc({
                "doctype": "File",
                "file_url": url,
                "attached_to_doctype": "Item",
                "attached_to_name": item_code,
                "is_private": 0,
            }).insert(ignore_permissions=True)
        except Exception:
            frappe.log_error(
                title="Shopware product import: image attach failed",
                message=frappe.get_traceback(),
            )
