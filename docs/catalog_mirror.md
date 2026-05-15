# Catalog Mirror

## What it is

Catalog Mirror keeps a 1:1 copy of an ERPNext **Item Group** subtree
in a backend (Shopware 6 or Medusa v2). Every IG under the configured
root is created, renamed, moved or deleted in the backend on every
sync. The IG→category mapping is stored on each IG as
`shopware_category_id` / `medusa_category_id` (installed by
`patches/setup_catalog_mirror`).

Use Catalog Mirror when the storefront's standard category structure
should track ERPNext. For rule-based groupings (`Sale`, `Bestseller`,
themed listings) use Smart Collections — see `smart_collections.md`.

## One mirror per (backend × root IG)

Each `Ecommerce Catalog Mirror` binds one `backend`, one
`root_item_group` and (on Shopware) one `target_sales_channel`.
Typical setup is one mirror per storefront.

Leave `external_root_id` empty on Shopware to auto-resolve from the
channel's `navigationCategoryId`; pin manually when adopting an
existing tree. `name_template` / `description_template` are Jinja
templates over each Item Group.

## Preview → Adopt → Apply

1. **Preview Mirror** — `apply_mirror(dry_run=True)`. No writes; the
   diff lists creates, updates, moves, no-ops, orphans and mapping
   drift.
2. **Adopt** — pin an existing backend category to an IG. Recorded
   as a `node_overrides` row so the binding survives renames.
3. **Apply Mirror Now** — `apply_mirror(dry_run=False)`. Persists the
   backend ID onto each IG as the node lands, so a partial failure
   still leaves the DB consistent with the backend.

## Orphan policy and running state

`orphan_policy` (`keep` / `delete` / `report`) decides what happens
to backend categories under the root with no matching IG. `delete`
issues `adapter.delete_node` during apply.

`apply_mirror` flips `sync_status='running'` and writes
`last_heartbeat_at` before any HTTP call. `recover_stale_mirrors`
sweeps mirrors stuck in `running` past the 30-minute window and flips
them to `error` so a crashed worker can't strand a mirror.

## Skipping IGs and per-item exceptions

`Item Group.catalog_mirror_skip` drops the flagged IG and its entire
subtree from every mirror walk — use it for IG branches owned by
Smart Collections.

Per-item overrides on `Item.ecommerce_channel_overrides` (child
doctype `Ecommerce Channel Override`) sit above Catalog Mirror in
the resolver: `mode=exclude` hides the item from a channel;
`mode=include` injects it into one the mirror would not have placed
it in.
