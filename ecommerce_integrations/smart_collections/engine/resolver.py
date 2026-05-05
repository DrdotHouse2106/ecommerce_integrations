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
        return f"NOT ({base}{set_predicate})"

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


_BUILDERS: dict = {
    "Item Group": _build_item_group_clause,
    "Ecommerce Property": _build_property_clause,
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
    """Group rules by ``group_id`` (OR within group), then join groups with
    ``combinator``."""
    if not rules:
        return "1=1"
    groups: dict[int, list[str]] = defaultdict(list)
    for r in rules:
        groups[r["group_id"]].append(_build_rule_clause(r))
    group_clauses = [f"({' OR '.join(parts)})" for parts in groups.values()]
    joiner = " AND " if (combinator or "AND") == "AND" else " OR "
    return joiner.join(group_clauses)


def resolve(collection) -> set[str]:
    """Return the set of ``Item.name`` matching this collection.

    Caches ``last_resolved_count`` and ``last_resolved_at`` on the collection
    when it has a name. Unsaved collections (preview/dry-run) skip the cache
    write but still return the resolved set.
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
