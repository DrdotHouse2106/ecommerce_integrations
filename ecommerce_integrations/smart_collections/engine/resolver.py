"""SQL-based resolver for Smart Collection rules.

Turns a collection's rules + combinator into a single SQL query that returns
the matching ``Item.name`` set. Standard filters (``disabled=0``,
``is_sales_item=1``, ``published_in_website=1``) are always applied so the
resolver only ever returns sellable, sync-able items.

Behaviour intentionally diverges from naive SQL three-valued-logic:
spec §4.4 requires that a property rule match an item only when the
property is *explicitly set* AND its value satisfies the operator. Items
without the property fall out — including for ``not_in``/``not_equals``.
This keeps "Schraubsystem" exclusions from accidentally pulling in
unclassified items.
"""

from collections import defaultdict

import frappe
from frappe.utils import now_datetime


def _split_csv(value: str | None) -> list[str]:
    if value is None:
        return []
    return [v.strip() for v in str(value).split(",") if v.strip()]


def _quote_list(values: list[str]) -> str:
    return ", ".join(frappe.db.escape(v) for v in values)


def _normalize_rule(rule) -> dict:
    """Accept either a Frappe child doc or a plain dict and return a dict
    with the resolver's expected keys."""

    def _get(key, default=None):
        if hasattr(rule, key):
            return getattr(rule, key)
        if isinstance(rule, dict):
            return rule.get(key, default)
        return default

    return {
        "rule_type": _get("rule_type"),
        "field_key": _get("field_key"),
        "operator": _get("operator"),
        "value": _get("value", "") or "",
        "group_id": _get("group_id") or 0,
        "negate": int(_get("negate") or 0),
    }


def _build_item_group_clause(rule: dict) -> str:
    op = rule["operator"]
    raw = rule["value"]

    if op == "descends_from":
        ig = frappe.db.escape(raw)
        return (
            "item.item_group IN ("
            "SELECT name FROM `tabItem Group` "
            f"WHERE lft >= (SELECT lft FROM `tabItem Group` WHERE name={ig}) "
            f"AND rgt <= (SELECT rgt FROM `tabItem Group` WHERE name={ig})"
            ")"
        )
    if op == "equals":
        return f"item.item_group = {frappe.db.escape(raw)}"
    if op == "not_equals":
        return f"item.item_group != {frappe.db.escape(raw)}"
    if op == "in":
        vals = _split_csv(raw)
        return f"item.item_group IN ({_quote_list(vals)})" if vals else "1=0"
    if op == "not_in":
        vals = _split_csv(raw)
        if not vals:
            return "1=1"
        return f"item.item_group NOT IN ({_quote_list(vals)})"
    raise ValueError(f"Unsupported Item Group operator: {op}")


def _build_property_clause(rule: dict) -> str:
    """Strict-NULL Ecommerce Property clause (spec §4.4).

    Items without the property never match, regardless of operator —
    including ``not_in`` / ``not_equals``. To pull in items lacking a
    property, combine ``not_in`` with ``is_empty`` via the same
    ``group_id`` (OR within group).
    """
    op = rule["operator"]
    name = rule.get("field_key")
    if not name:
        raise ValueError("Ecommerce Property rule requires field_key")
    name_q = frappe.db.escape(name)
    raw = rule["value"] or ""

    base = (
        "EXISTS (SELECT 1 FROM `tabItem Ecommerce Property` p "
        f"WHERE p.parent = item.name AND p.property_name = {name_q}"
    )
    set_predicate = " AND p.property_value IS NOT NULL AND p.property_value != ''"

    if op == "is_set":
        return base + set_predicate + ")"
    if op == "is_empty":
        return f"NOT ({base}{set_predicate}))"

    if op == "in":
        vals = _split_csv(raw)
        if not vals:
            return "1=0"
        return base + f" AND p.property_value IN ({_quote_list(vals)}))"
    if op == "not_in":
        vals = _split_csv(raw)
        if not vals:
            return "1=0"
        return base + f" AND p.property_value NOT IN ({_quote_list(vals)}))"
    if op == "equals":
        return base + f" AND p.property_value = {frappe.db.escape(raw)})"
    if op == "not_equals":
        return base + f" AND p.property_value != {frappe.db.escape(raw)})"
    if op == "contains":
        return base + f" AND p.property_value LIKE {frappe.db.escape(f'%{raw}%')})"
    if op == "regex":
        return base + f" AND p.property_value REGEXP {frappe.db.escape(raw)})"
    raise ValueError(f"Unsupported Ecommerce Property operator: {op}")


def _build_direct_column_clause(rule: dict, column: str) -> str:
    """Strict-NULL clause for plain ``tabItem`` columns (manufacturer, brand, …).

    Mirrors the property semantics: ``not_in``/``not_equals`` exclude rows
    whose column is NULL/empty, so excluding "BrandA" never silently pulls
    in items with no brand at all.
    """
    op = rule["operator"]
    raw = rule["value"] or ""
    col = f"item.`{column}`"

    if op == "is_set":
        return f"({col} IS NOT NULL AND {col} != '')"
    if op == "is_empty":
        return f"({col} IS NULL OR {col} = '')"
    if op == "in":
        vals = _split_csv(raw)
        return f"{col} IN ({_quote_list(vals)})" if vals else "1=0"
    if op == "not_in":
        vals = _split_csv(raw)
        if not vals:
            return "1=0"
        return f"({col} IS NOT NULL AND {col} != '' AND {col} NOT IN ({_quote_list(vals)}))"
    if op == "equals":
        return f"{col} = {frappe.db.escape(raw)}"
    if op == "not_equals":
        return f"({col} IS NOT NULL AND {col} != '' AND {col} != {frappe.db.escape(raw)})"
    if op == "contains":
        return f"{col} LIKE {frappe.db.escape(f'%{raw}%')}"
    if op == "regex":
        return f"{col} REGEXP {frappe.db.escape(raw)}"
    raise ValueError(f"Unsupported direct-column operator: {op}")


# ``Item Field`` rules are direct-column too but accept a user-chosen field
# via ``field_key``. Keep this list narrow on purpose — opening the whole
# ``tabItem`` schema lets users write rules against fields that change
# frequently or contain PII (e.g. ``description``), and a typo'd field
# leaks into a SQL error rather than a clean validation message.
_ITEM_FIELD_WHITELIST = frozenset(
    {
        "item_code",
        "item_name",
        "item_group",
        "manufacturer",
        "brand",
        "stock_uom",
        "country_of_origin",
    }
)


def _build_item_field_clause(rule: dict) -> str:
    field = rule.get("field_key")
    if field not in _ITEM_FIELD_WHITELIST:
        raise ValueError(
            f"Item Field {field!r} not in whitelist; allowed: "
            f"{sorted(_ITEM_FIELD_WHITELIST)}"
        )
    return _build_direct_column_clause(rule, field)


def _build_stock_clause(rule: dict) -> str:
    """Stock availability via ``tabBin``. Only ``is_set``/``is_empty`` make
    sense — quantity comparisons would need their own operator surface and
    stock is a coarse "in stock anywhere" check today."""
    op = rule["operator"]
    qty_expr = (
        "(SELECT COALESCE(SUM(actual_qty), 0) FROM `tabBin` b "
        "WHERE b.item_code = item.name)"
    )
    if op == "is_set":
        return f"{qty_expr} > 0"
    if op == "is_empty":
        return f"{qty_expr} <= 0"
    raise ValueError(
        f"Unsupported Stock operator: {op} (only is_set/is_empty supported)"
    )


def _build_manufacturer_clause(rule: dict) -> str:
    """Manufacturer is stored in the ``tabItem Manufacturer`` child table —
    one item can be linked to multiple manufacturers, so we query EXISTS
    against the child rows and keep strict-NULL semantics for negation."""
    op = rule["operator"]
    raw = rule["value"] or ""
    base = (
        "EXISTS (SELECT 1 FROM `tabItem Manufacturer` im "
        "WHERE im.item_code = item.name"
    )
    if op == "is_set":
        return base + ")"
    if op == "is_empty":
        return f"NOT ({base})"
    if op == "in":
        vals = _split_csv(raw)
        if not vals:
            return "1=0"
        return base + f" AND im.manufacturer IN ({_quote_list(vals)}))"
    if op == "not_in":
        vals = _split_csv(raw)
        if not vals:
            return "1=0"
        return base + f" AND im.manufacturer NOT IN ({_quote_list(vals)}))"
    if op == "equals":
        return base + f" AND im.manufacturer = {frappe.db.escape(raw)})"
    if op == "not_equals":
        return base + f" AND im.manufacturer != {frappe.db.escape(raw)})"
    if op == "contains":
        return base + f" AND im.manufacturer LIKE {frappe.db.escape(f'%{raw}%')})"
    if op == "regex":
        return base + f" AND im.manufacturer REGEXP {frappe.db.escape(raw)})"
    raise ValueError(f"Unsupported Manufacturer operator: {op}")


_BUILDERS: dict = {
    "Item Group": _build_item_group_clause,
    "Ecommerce Property": _build_property_clause,
    "Manufacturer": _build_manufacturer_clause,
    "Brand": lambda r: _build_direct_column_clause(r, "brand"),
    "Item Field": _build_item_field_clause,
    "Stock": _build_stock_clause,
}


def _build_rule_clause(rule: dict) -> str:
    builder = _BUILDERS.get(rule["rule_type"])
    if builder is None:
        raise NotImplementedError(
            f"Rule type {rule['rule_type']!r} not implemented yet"
        )
    clause = builder(rule)
    if rule["negate"]:
        return f"NOT ({clause})"
    return clause


def _combine_groups(rules: list[dict], combinator: str) -> str:
    """Combine rule clauses using the spec §4.2 grouping semantics.

    ``group_id=0`` (the default) means *independent* — each such rule forms
    its own implicit group and combines with the others via ``combinator``.
    A non-zero ``group_id`` puts the rule into a shared OR-group with its
    siblings; the resulting OR-clause then combines with other groups via
    ``combinator``.

    Without this, three default rules in the form (Item Group=X, System=Y,
    Material=Z) would be OR'd into a union — but the design intent for the
    default case is AND (intersection).
    """
    if not rules:
        return "1=1"
    independents: list[list[str]] = []
    explicit: dict[int, list[str]] = defaultdict(list)
    for r in rules:
        clause = _build_rule_clause(r)
        if r["group_id"]:
            explicit[r["group_id"]].append(clause)
        else:
            independents.append([clause])
    all_groups = independents + list(explicit.values())
    group_clauses = [f"({' OR '.join(parts)})" for parts in all_groups]
    joiner = " AND " if (combinator or "AND") == "AND" else " OR "
    return joiner.join(group_clauses)


def resolve(collection, *, persist_stats: bool = True) -> set[str]:
    """Return the set of ``Item.name`` matching this collection.

    Caches ``last_resolved_count`` / ``last_resolved_at`` on the collection
    when it has a name and ``persist_stats`` is true. ``dry_run`` sets
    ``persist_stats=False`` so previews don't side-effect the cache.
    """
    rules = [_normalize_rule(r) for r in (collection.rules or [])]
    rule_sql = _combine_groups(rules, getattr(collection, "rule_combinator", "AND"))

    sql = (
        "SELECT item.name FROM `tabItem` item "
        "WHERE item.disabled = 0 "
        "AND item.is_sales_item = 1 "
        "AND item.published_in_website = 1 "
        f"AND ({rule_sql})"
    )
    rows = frappe.db.sql(sql, as_dict=False)
    items = {row[0] for row in rows}

    if persist_stats:
        collection.last_resolved_count = len(items)
        collection.last_resolved_at = now_datetime()
        if getattr(collection, "name", None):
            frappe.db.set_value(
                "Ecommerce Smart Collection",
                collection.name,
                {
                    "last_resolved_count": collection.last_resolved_count,
                    "last_resolved_at": collection.last_resolved_at,
                },
                update_modified=False,
            )
    return items


def dry_run(collection) -> dict:
    """Preview a collection's resolution without touching its cache.

    Returns:
        ``count`` — total matching items.
        ``sample_items`` — up to 50 items as ``[{name, item_name, item_group}]``.
        ``breakdown_by_rule`` — for each rule, how many items it would match
            on its own. Useful to spot the rule that's wiping out the result.
    """
    matched = resolve(collection, persist_stats=False)
    sample_names = sorted(matched)[:50]
    sample_rows: list[dict] = []
    if sample_names:
        sample_rows = frappe.db.sql(
            "SELECT name, item_name, item_group FROM `tabItem` "
            f"WHERE name IN ({_quote_list(sample_names)})",
            as_dict=True,
        )

    breakdown: list[dict] = []
    for idx, raw_rule in enumerate(collection.rules or []):
        r = _normalize_rule(raw_rule)
        single = frappe.new_doc("Ecommerce Smart Collection")
        single.title = "_dry_run_per_rule"
        single.rule_combinator = "AND"
        single.append("rules", r)
        per_rule_count = len(resolve(single, persist_stats=False))
        breakdown.append(
            {
                "rule_index": idx,
                "rule_type": r["rule_type"],
                "field_key": r["field_key"],
                "operator": r["operator"],
                "value": r["value"],
                "matching_count": per_rule_count,
            }
        )

    return {
        "count": len(matched),
        "sample_items": sample_rows,
        "breakdown_by_rule": breakdown,
    }
