"""Composite index on Item Ecommerce Property for the Smart Collections resolver.

Resolver SQL (spec §4.3) hits the table once per Ecommerce Property rule with a
``WHERE parent=item.name AND property_name=? AND property_value [op] ?`` shape.
Without a covering index, that's a full scan over every property row per item;
this index keeps lookups index-bound.

Idempotent: checks ``information_schema.statistics`` before issuing CREATE.
``property_value`` is ``text`` so the index uses a 64-character prefix.
"""

import frappe


INDEX_NAME = "idx_iep_resolver_lookup"
DOCTYPE = "Item Ecommerce Property"
TABLE = f"tab{DOCTYPE}"


def execute():
	if not frappe.db.table_exists(DOCTYPE):
		return

	exists = frappe.db.sql(
		"""
		SELECT 1 FROM information_schema.statistics
		WHERE TABLE_SCHEMA = DATABASE()
		  AND TABLE_NAME = %s
		  AND INDEX_NAME = %s
		LIMIT 1
		""",
		(TABLE, INDEX_NAME),
	)
	if exists:
		return

	frappe.db.sql_ddl(
		f"CREATE INDEX `{INDEX_NAME}` ON `{TABLE}` "
		f"(parent, property_name, property_value(64))"
	)
