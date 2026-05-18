"""Constants shared across product_sync. Kept in one place so adapters
and the orchestrator agree on the wire vocabulary (action codes,
scope modes, policy values).

The names mirror the upstream :mod:`catalog_mirror.constants` style so a
reader who already knows Catalog Mirror sees the same identifiers here.
Only the doctype names refer to the Product Sync masters — everything
else is intentionally backend-agnostic.
"""

from __future__ import annotations

# Backends (mirror catalog_mirror.constants for cross-reference). Keeping
# the two modules in lockstep means a new backend (e.g. "Magento") only
# has to be added once per module rather than fanned out through the
# code base.
BACKEND_SHOPWARE = "Shopware"
BACKEND_MEDUSA = "Medusa"

KNOWN_BACKENDS = (BACKEND_SHOPWARE, BACKEND_MEDUSA)

# Backend → Module Def name used as the ``integration`` value on
# ``tabEcommerce Item``. ``Ecommerce Item.integration`` is a Link to
# ``Module Def`` and must match the shipped module folder name —
# ``shopware6`` not ``shopware``, ``medusa`` not ``Medusa``.
BACKEND_TO_INTEGRATION_KEY = {
    BACKEND_SHOPWARE: "shopware6",
    BACKEND_MEDUSA: "medusa",
}

# Doctype names — referenced as strings everywhere so this module stays
# importable before the doctype is migrated in. The values must match
# the JSON files shipped under ``product_sync/doctype/``.
SYNC_DOCTYPE = "Ecommerce Product Sync"
CHANNEL_DOCTYPE = "Ecommerce Product Sync Channel"
FILTER_RULE_DOCTYPE = "Ecommerce Product Sync Filter Rule"
OVERRIDE_DOCTYPE = "Ecommerce Product Sync Override"

LOG_DOCTYPE = "Ecommerce Integration Log"

# Scope modes — must match the Select options on the master doctype.
# The walker dispatches on these strings, so renaming one of them
# without touching the JSON would silently turn a scope into a no-op.
SCOPE_CATALOG_MIRROR = "Catalog Mirror"
SCOPE_ITEM_GROUP = "Item Group Subtree"
SCOPE_SMART_COLLECTION = "Smart Collection"
SCOPE_CUSTOM_FILTER = "Custom Filter"
SCOPE_ALL = "All"

# Action codes used in the diff buckets. The strings double as the JS
# contract: the form's preview dialog uses them verbatim as bucket keys.
ACTION_CREATE = "create"
ACTION_UPDATE = "update"
ACTION_NOOP = "noop"
ACTION_DEACTIVATE = "deactivate"
ACTION_DELETE = "delete"

# Orphan policy values — what to do with backend products that no longer
# match any Item in the configured scope.
ORPHAN_KEEP = "keep"
ORPHAN_DEACTIVATE = "deactivate"
ORPHAN_DELETE = "delete"
ORPHAN_REPORT = "report"

# Update policy — when an Item already exists in the backend, decide
# whether to push every run or only on field drift.
UPDATE_ALWAYS = "always"
UPDATE_ONLY_IF_CHANGED = "only_if_changed"
UPDATE_MANUAL_REVIEW = "manual_review"

# Conflict policy — when more than one active Sync claims the same Item
# at the same priority level, how to break the tie.
CONFLICT_MANUAL_REVIEW = "manual_review"
CONFLICT_LAST_WINS = "last_wins"
CONFLICT_SKIP = "skip"

# Override modes — per-item escape hatch on the Sync's Override table.
OVERRIDE_PIN = "pin"
OVERRIDE_SKIP = "skip"
OVERRIDE_CUSTOM = "custom"

# Image strategies — how to ship product imagery to the backend.
IMAGE_URL_ONLY = "url_only"
IMAGE_URL_WITH_BYTES_FALLBACK = "url_with_bytes_fallback"
IMAGE_BYTES_ONLY = "bytes_only"

# Description source — where the marketing copy comes from.
DESCRIPTION_ITEM = "item_description"
DESCRIPTION_AI = "ai_generated"
DESCRIPTION_CUSTOM = "custom_template"

# Price strategies — how the outbound price is computed.
PRICE_CHANNEL_PRICE_LIST = "channel_price_list"
PRICE_ITEM_STANDARD = "item_standard_rate"
PRICE_CUSTOM_MARKUP = "custom_markup"

# Sync status values — used on the master doctype and in run results.
STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_OK = "ok"
STATUS_ERROR = "error"
STATUS_PARTIAL = "partial"
