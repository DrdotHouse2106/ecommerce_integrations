# Smart Collections

## What it is

A Smart Collection is a rule-based, ad-hoc grouping of items: every
Item that matches the rules is pushed onto each enabled target. Use
this for groupings the Item Group tree does not encode —
promotional buckets (`Sale`), themed listings (`Bestseller`),
seasonal selections.

## What it is NOT for

Not a 1:1 mirror of the Item Group tree. The single-rule
"`Item Group descends_from X`" pattern was the old workaround; use
**Catalog Mirror** for that (see `catalog_mirror.md`). The patch
`migrate_shopware_umbrellas_to_catalog_mirror` auto-converts the
common case and disables the SC's Shopware target non-destructively.

## Rule shape

Each `Ecommerce Smart Collection` carries a `rule_combinator`
(`AND` / `OR`) and a child table of `Ecommerce Smart Collection Rule`
rows. Each rule has `rule_type` (`Item Group`, `Ecommerce Property`,
`Manufacturer`, `Item Field`, `Stock`, `Brand`), an `operator`
(`equals`, `not_equals`, `in`, `not_in`, `descends_from`, `contains`,
`regex`, `is_set`, `is_empty`), an optional `field_key` and a
`value`. `group_id` lets rules combine with OR within a group while
groups combine via the parent's combinator. `negate` inverts a rule;
NULL handling is strict (a NULL value never matches, so `NOT(rule)`
is not the operator's negation when the property is unset).

## Preview Sync and Adopt

The form's **Preview Sync** dialog returns, per enabled target, the
resolved item set, the action against the live backend
(`CREATE` / `UPDATE` / `NOOP`), add/remove/keep counts vs. the local
snapshot, and Adopt candidates when the target has no `external_id`.
Live syncs run through `sync_collection` / `sync_due_collections`;
each target carries its own `sync_status` and `last_error`, so one
target's failure does not abort the others.

## Channel-visibility role

Smart Collections feed the **unified resolver** in
`catalog_mirror/resolver.py` together with Catalog Mirror placements
and per-item overrides. Precedence: per-item override > mirror >
smart collection; on a tie the highest visibility wins.

The legacy `smart_collections.channel_visibility` module is now a
delegator that reshapes the resolver's typed result back to the
legacy dict form for downstream callers.
