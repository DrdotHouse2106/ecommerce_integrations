"""Shared identifiers for the Catalog Mirror module.

Centralises backend names so adding a new backend or changing a label
only touches this module.
"""


BACKEND_MEDUSA = "Medusa"
BACKEND_SHOPWARE = "Shopware"

KNOWN_BACKENDS = (BACKEND_MEDUSA, BACKEND_SHOPWARE)


# Diff action labels — kept here so the JS contract is centrally
# documented and tests can assert on exact strings without rummaging
# through engine internals.
ACTION_CREATE = "create"
ACTION_UPDATE = "update"
ACTION_MOVE = "move"
ACTION_NOOP = "noop"

# Orphan policy values (mirror the doctype Select).
ORPHAN_KEEP = "keep"
ORPHAN_DELETE = "delete"
ORPHAN_REPORT = "report"
