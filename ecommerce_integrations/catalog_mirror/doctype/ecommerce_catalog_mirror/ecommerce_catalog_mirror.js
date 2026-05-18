// Catalog Mirror form — rich tree-diff preview dialog (Phase 3).
//
// Mirrors the Smart Collections preview dialog's renderer style
// (`ecommerce_smart_collection.js`): same `__()` calls, same
// `frappe.utils.escape_html`, same indicator-pill vocabulary, so an
// operator who learnt one already understands the other.
//
// Icons: Frappe v16 ships Lucide as an SVG sprite (loaded globally on
// every desk page), so `<svg class="icon icon-sm"><use href="#icon-X">`
// works without any bundle changes. ``_icon()`` is the local helper so
// every call site stays terse.


function _icon(name, size, color) {
    const cls = 'icon icon-' + (size || 'sm');
    const style = color ? `color:${color};vertical-align:middle` : 'vertical-align:middle';
    return `<svg class="${cls}" style="${style}"><use href="#icon-${name}"></use></svg>`;
}


// CSS-mask-image icon variant: ONE DOM node per icon, the SVG path is
// parsed once and cached as a mask. ~10x cheaper than 700+ <svg><use>
// references in a scrollable table — the orphan/tree action columns
// use this; banners and section headers stay on _icon() because the
// count there is single-digit.
//
// Stroke style mirrors Lucide's default (stroke-width:2, round caps).
// Color comes from the parent's ``color`` via ``currentColor``.
const _CM_MASK_ICONS = {
    link:  "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'><path d='M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71'/><path d='M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71'/></svg>",
    trash: "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'><path d='M3 6h18'/><path d='M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6'/><path d='M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2'/><line x1='10' x2='10' y1='11' y2='17'/><line x1='14' x2='14' y1='11' y2='17'/></svg>",
    check: "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'><path d='M20 6 9 17l-5-5'/></svg>",
};


function _inject_cm_styles_once() {
    if (document.getElementById('cm-dialog-styles')) return;
    const rules = [`.cm-i {
        display: inline-block; width: 14px; height: 14px;
        background-color: currentColor;
        -webkit-mask-position: center; mask-position: center;
        -webkit-mask-size: contain;    mask-size: contain;
        -webkit-mask-repeat: no-repeat; mask-repeat: no-repeat;
        vertical-align: -2px;
    }`];
    for (const [key, svg] of Object.entries(_CM_MASK_ICONS)) {
        const data = encodeURIComponent(svg).replace(/%20/g, ' ');
        rules.push(`.cm-i-${key} {
            -webkit-mask-image: url("data:image/svg+xml;utf8,${data}");
                    mask-image: url("data:image/svg+xml;utf8,${data}");
        }`);
    }
    const style = document.createElement('style');
    style.id = 'cm-dialog-styles';
    style.textContent = rules.join('\n');
    document.head.appendChild(style);
}


// Tiny inline icon spans for the per-row action buttons. ``cls`` is
// the mask-class suffix (link / trash / check). All buttons in the
// orphan table use these via classes registered in
// ``_inject_cm_styles_once``.
function _ci(cls) {
    return `<i class="cm-i cm-i-${cls}"></i>`;
}

frappe.ui.form.on('Ecommerce Catalog Mirror', {
    onload(frm) {
        _populate_sales_channel_options(frm);
    },
    backend(frm) {
        _populate_sales_channel_options(frm);
    },
    refresh(frm) {
        _populate_sales_channel_options(frm);
        if (frm.is_new()) return;
        _render_dashboard_indicators(frm);
        _render_intro(frm);
        _render_header_buttons(frm);
    },
});


// Populate the target_sales_channel{,_data} autocompletes from the
// backend's setting doctype. The field can't be a Link because both
// Shopware Sales Channel and Medusa Sales Channel are istable=1 —
// Frappe's link loader trips "Insufficient Permission" on form open.
// Stored value stays the bare UUID.
//
// We call ``set_data()`` on the control directly (not just
// ``set_df_property('options', ...)``) because Frappe v16's
// Autocomplete only reads ``df.options`` inside ``make_input`` — once
// the control is built, only ``set_data()`` reaches the internal
// ``_data`` array that ``format_for_input`` consults to render the
// friendly label for a stored UUID. After populating ``_data`` we
// re-set the input so the existing doc value picks up its label.
function _populate_sales_channel_options(frm) {
    if (frm.doc.backend === 'Shopware') {
        _load_channels_into(frm, 'target_sales_channel',
            'ecommerce_integrations.catalog_mirror.api.list_shopware_sales_channels');
    } else if (frm.doc.backend === 'Medusa') {
        _load_channels_into(frm, 'target_sales_channel_data',
            'ecommerce_integrations.catalog_mirror.api.list_medusa_sales_channels');
    }
}


function _load_channels_into(frm, fieldname, method) {
    const field = frm.fields_dict[fieldname];
    if (!field || typeof field.set_data !== 'function') return;
    frappe.call({
        method: method,
        callback(r) {
            const opts = r.message || [];
            field.df.options = opts;
            field.set_data(opts);
            // Re-render the input so the stored UUID picks up its label.
            const current = frm.doc[fieldname];
            if (current && typeof field.set_input === 'function') {
                field.set_input(current);
            }
        },
    });
}


function _render_dashboard_indicators(frm) {
    if (frm.doc.sync_status === 'running') {
        const since = frm.doc.last_heartbeat_at
            ? frappe.datetime.comment_when(frm.doc.last_heartbeat_at)
            : '—';
        frm.dashboard.add_indicator(__('Sync running since {0}', [since]), 'blue');
    } else if (frm.doc.sync_status === 'error') {
        const snippet = (frm.doc.last_error || '').split('\n')[0].slice(0, 120);
        frm.dashboard.add_indicator(
            snippet ? __('Sync error: {0}', [snippet]) : __('Sync error'),
            'red',
        );
    } else if (frm.doc.sync_status === 'ok') {
        frm.dashboard.add_indicator(__('Last sync OK'), 'green');
    }
}


function _render_intro(frm) {
    const parts = [];
    if (frm.doc.root_item_group) parts.push(__('Root: {0}', [frm.doc.root_item_group]));
    if (frm.doc.target_sales_channel) parts.push(__('Channel: {0}', [frm.doc.target_sales_channel]));
    if (frm.doc.last_synced_at) {
        parts.push(__('Last synced: {0}', [frappe.datetime.comment_when(frm.doc.last_synced_at)]));
    }
    if (parts.length) frm.set_intro(parts.join(' · '), 'blue');
}


function _render_header_buttons(frm) {
    frm.add_custom_button(__('Preview Sync'), () => _open_preview(frm)).addClass('btn-primary');
    frm.add_custom_button(__('Apply Live'), function () {
        // Double-confirm — apply mutates the live backend.
        frappe.confirm(
            __('Apply this mirror against the live backend now?'),
            () => frappe.confirm(
                __('Really apply now? This creates / updates / deletes backend categories.'),
                () => _apply_live(frm),
            ),
        );
    });
}


function _open_preview(frm) {
    frappe.call({
        method: 'ecommerce_integrations.catalog_mirror.api.preview_mirror',
        args: { mirror: frm.doc.name },
        freeze: true,
        freeze_message: __('Building preview…'),
        callback(r) {
            if (r.message) _show_preview_dialog(frm, r.message);
        },
    });
}


function _apply_live(frm) {
    frappe.call({
        method: 'ecommerce_integrations.catalog_mirror.api.apply_mirror_now',
        args: { mirror: frm.doc.name },
        freeze: true,
        freeze_message: __('Applying mirror…'),
        callback(r) {
            if (!r.message) return;
            const res = r.message;
            frappe.show_alert({
                message: __('Status: {0} — created: {1}, updated: {2}, moved: {3}, deleted: {4}', [
                    res.status, res.created || 0, res.updated || 0, res.moved || 0, res.deleted || 0,
                ]),
                indicator: res.status === 'ok' ? 'green' : 'red',
            });
            frm.reload_doc();
        },
    });
}


// ── Preview dialog ──────────────────────────────────────────────────

function _show_preview_dialog(frm, plan) {
    _inject_cm_styles_once();
    const d = new frappe.ui.Dialog({
        title: __('Preview Sync: {0}', [plan.title || frm.doc.name]),
        size: 'large',
        fields: [{ fieldname: 'preview_html', fieldtype: 'HTML' }],
        primary_action_label: __('Apply (Live)'),
        primary_action() {
            const c = _bucket_counts(plan);
            frappe.confirm(
                __('Apply this plan to {0}? Creates: {1}, Updates: {2}, Moves: {3}, Orphans handled: {4}.',
                    [plan.title || frm.doc.name, c.create, c.update, c.move, c.orphan]),
                () => { d.hide(); _apply_live(frm); },
            );
        },
    });

    // Secondary "Refresh Preview" re-runs preview_mirror so the
    // operator can iterate after using Adopt buttons without closing
    // the dialog.
    d.set_secondary_action_label(__('Refresh Preview'));
    d.set_secondary_action(() => _refresh_preview_inplace(frm, d));

    if (plan.skipped) {
        d.fields_dict.preview_html.$wrapper.html(
            `<div class="alert alert-warning">${__('Skipped: {0}',
                [frappe.utils.escape_html(plan.skip_reason || '')])}</div>`,
        );
        d.show();
        return;
    }

    d._plan = plan;
    d._frm = frm;
    d.fields_dict.preview_html.$wrapper.html(_render_preview(plan, frm));
    _wire_dialog_handlers(frm, d);
    d.show();
}


// Event delegation — the inner HTML is rebuilt on every Refresh, so
// listeners go on the (persistent) wrapper, not the rendered nodes.
function _wire_dialog_handlers(frm, d) {
    const $w = d.fields_dict.preview_html.$wrapper;

    $w.on('click', '.cm-toggle', function () {
        const $kids = $(this).closest('li').children('ul');
        if (!$kids.length) return;
        const open = $kids.is(':visible');
        $kids.toggle();
        $(this).text(open ? '▸' : '▾');
    });

    $w.on('click', '.cm-adopt-create', function () {
        _open_adopt_dialog_for_create(frm, d, $(this).data('ig'), $(this).data('parent-ext') || '');
    });

    $w.on('click', '.cm-orphan-adopt', function () {
        _open_adopt_dialog_for_orphan(frm, d, $(this).data('ext'), $(this).data('name') || '');
    });

    $w.on('click', '.cm-orphan-delete', function () {
        const ext = $(this).data('ext');
        frappe.confirm(
            __('Delete backend category {0}? This cannot be undone.', [ext]),
            () => _orphan_action(frm, d, ext, 'delete'),
        );
    });

    $w.on('click', '.cm-orphan-keep', function () {
        _orphan_action(frm, d, $(this).data('ext'), 'keep');
    });

    $w.on('click', '.cm-drift-clear', function () {
        _clear_drift_mapping(frm, d, $(this).data('ig'));
    });

    $w.on('click', '.cm-adopt-by-path', function () {
        _auto_adopt_by_path(frm, d);
    });

    // Tree-table: action filter chips. Tabs swap the "active" highlight
    // and toggle row visibility via the data-action attribute; no
    // re-render needed.
    $w.on('click', '.cm-tree-filter', function () {
        const filter = $(this).data('filter');
        $w.find('.cm-tree-filter').removeClass('btn-primary').addClass('btn-default');
        $(this).removeClass('btn-default').addClass('btn-primary');
        $w.find('.cm-tree-row').each(function () {
            const action = $(this).data('action');
            const search_q = ($w.find('.cm-tree-search').val() || '').toLowerCase().trim();
            const matches_filter = filter === 'all' || filter === action;
            const matches_search = !search_q || ($(this).data('search') || '').includes(search_q);
            $(this).toggle(matches_filter && matches_search);
        });
    });

    // Tree-table: free-text search across the path + proposed name.
    $w.on('input', '.cm-tree-search', function () {
        const q = ($(this).val() || '').toLowerCase().trim();
        const active = $w.find('.cm-tree-filter.btn-primary').data('filter') || 'all';
        $w.find('.cm-tree-row').each(function () {
            const action = $(this).data('action');
            const matches_filter = active === 'all' || active === action;
            const matches_search = !q || ($(this).data('search') || '').includes(q);
            $(this).toggle(matches_filter && matches_search);
        });
    });

    // Orphans: free-text search.
    $w.on('input', '.cm-orphans-search', function () {
        const q = ($(this).val() || '').toLowerCase().trim();
        $w.find('.cm-orphans-table tbody tr').each(function () {
            $(this).toggle(!q || ($(this).data('search') || '').includes(q));
        });
    });

    // Orphans: header select-all toggles all row checkboxes.
    $w.on('change', '.cm-orphan-cb-all', function () {
        const checked = $(this).prop('checked');
        $w.find('.cm-orphan-cb').prop('checked', checked);
        _update_orphan_bulk_state($w);
    });

    $w.on('change', '.cm-orphan-cb', function () {
        _update_orphan_bulk_state($w);
    });

    $w.on('click', '.cm-orphans-bulk-delete', function () {
        const exts = $w.find('.cm-orphan-cb:checked').map(function () { return $(this).data('ext'); }).get();
        if (!exts.length) return;
        frappe.confirm(
            __('Delete {0} backend categories? This action cannot be undone.', [exts.length]),
            () => _bulk_orphan_action(frm, d, exts, 'delete'),
        );
    });

    $w.on('click', '.cm-orphans-bulk-keep', function () {
        const exts = $w.find('.cm-orphan-cb:checked').map(function () { return $(this).data('ext'); }).get();
        if (!exts.length) return;
        _bulk_orphan_action(frm, d, exts, 'keep');
    });
}


function _update_orphan_bulk_state($w) {
    const checked = $w.find('.cm-orphan-cb:checked').length;
    const total = $w.find('.cm-orphan-cb').length;
    $w.find('.cm-orphans-bulk-delete, .cm-orphans-bulk-keep').prop('disabled', checked === 0);
    $w.find('.cm-orphans-selcount').text(__('{0} selected', [checked]));
    $w.find('.cm-orphan-cb-all').prop({
        checked: checked > 0 && checked === total,
        indeterminate: checked > 0 && checked < total,
    });
}


// Bulk orphan action: fire each set_orphan_action sequentially so the
// server-side log stays in order and a single failure doesn't
// abandon the rest unpaired. Sequential is fine — these are local
// metadata writes, not Shopware round-trips.
function _bulk_orphan_action(frm, parent_dialog, exts, action) {
    let done = 0;
    const total = exts.length;
    const tick = () => {
        if (done >= total) {
            frappe.show_alert({
                message: __('{0} categories: {1}', [total, action === 'delete' ? __('Marked for deletion') : __('Behalten')]),
                indicator: action === 'delete' ? 'red' : 'blue',
            });
            _refresh_preview_inplace(frm, parent_dialog);
            return;
        }
        frappe.call({
            method: 'ecommerce_integrations.catalog_mirror.api.set_orphan_action',
            args: { mirror: frm.doc.name, external_id: exts[done++], action },
            callback: tick,
        });
    };
    tick();
}


// One-shot path-based adoption. Calls the server endpoint, surfaces
// the result counts, then refreshes the preview so the operator sees
// the diff collapse from "create everything" to "mostly noops".
function _auto_adopt_by_path(frm, parent_dialog) {
    frappe.confirm(
        __('Match existing backend categories by path? ' +
            'Nicht-eindeutige Treffer und bereits zugeordnete Artikelgruppen werden übersprungen. ' +
            'Diese Aktion ist idempotent.'),
        () => frappe.call({
            method: 'ecommerce_integrations.catalog_mirror.api.auto_adopt_by_path',
            args: { mirror: frm.doc.name },
            freeze: true,
            freeze_message: __('Pfade werden abgeglichen…'),
            callback(r) {
                if (!r.message) return;
                const m = r.message;
                frappe.msgprint({
                    title: __('Per-Pfad-Zuordnung abgeschlossen'),
                    message: __(
                        '<table class="table table-sm" style="margin-bottom:0">' +
                            '<tr><th class="text-right">{0}</th><td>{1}</td></tr>' +
                            '<tr><th class="text-right">{2}</th><td>{3}</td></tr>' +
                            '<tr><th class="text-right">{4}</th><td>{5}</td></tr>' +
                            '<tr><th class="text-right">{6}</th><td>{7}</td></tr>' +
                        '</table>',
                        [
                            __('Adoptiert'), m.adopted,
                            __('Already mapped (skipped)'), m.skipped_already_mapped,
                            __('Ambiguous (skipped)'), m.ambiguous,
                            __('Kein Pfad-Treffer'), m.no_match,
                        ],
                    ),
                    indicator: m.adopted > 0 ? 'green' : 'orange',
                });
                _refresh_preview_inplace(frm, parent_dialog);
            },
        }),
    );
}




// ── Renderers ───────────────────────────────────────────────────────

function _bucket_counts(plan) {
    return {
        create: (plan.creates || []).length,
        update: (plan.updates || []).length,
        move: (plan.moves || []).length,
        noop: (plan.noops || []).length,
        orphan: (plan.orphans || []).length,
        drift: (plan.mapping_drift || []).length,
    };
}


function _render_preview(plan, frm) {
    const c = _bucket_counts(plan);
    const orphan_policy = (frm && frm.doc && frm.doc.orphan_policy) || 'keep';

    return _render_summary_card(plan)
        + _render_first_sync_banner(plan, c)
        + _render_action_banners(plan, c)
        + _render_metrics_bar(c)
        + _render_notes(plan)
        + _render_tree_section(plan)
        + _render_orphans_section(plan, orphan_policy)
        + _render_drift_section(plan);
}


// Compact header with all the "what am I looking at" context in one
// glance — backend, channel, root mapping, live root status.
function _render_summary_card(plan) {
    const root_live = plan.external_root_id
        ? `<code style="font-size:11px">${frappe.utils.escape_html(plan.external_root_id)}</code>`
        : `<em class="text-muted">${__('not yet resolved')}</em>`;
    const status_pill = plan.unresolved_external_root
        ? `<span class="indicator-pill yellow">${__('Root not resolved')}</span>`
        : `<span class="indicator-pill green">${__('Root resolved')}</span>`;
    return `
        <div style="margin-bottom:1em;padding:0.75em 1em;
                    background:var(--bg-color, #fafbfc);
                    border:1px solid var(--border-color);
                    border-radius:6px">
            <div style="display:flex;justify-content:space-between;align-items:center;gap:1em;flex-wrap:wrap">
                <div>
                    <div style="font-size:14px">
                        <strong>${frappe.utils.escape_html(plan.root_item_group || '—')}</strong>
                        <span class="text-muted">→</span> ${root_live}
                    </div>
                    <div class="text-muted small" style="margin-top:0.25em">
                        ${__('Zielsystem')}: <strong>${frappe.utils.escape_html(plan.backend)}</strong>
                        · ${__('Kanal')}: <code style="font-size:11px">${frappe.utils.escape_html(plan.target_sales_channel || '—')}</code>
                    </div>
                </div>
                <div>${status_pill}</div>
            </div>
        </div>`;
}


// Big-green-button banner for the typical first-sync scenario where
// the operator has an existing curated backend tree whose names match
// the ERP Item Groups. The default differ refuses name-based matching
// (by design: silent re-link after rename is dangerous), so we surface
// the one-shot adopter here instead. Conditions kept conservative:
// many creates + many orphans is the unmistakable "first sync against
// a hand-curated tree" pattern.
function _render_first_sync_banner(plan, c) {
    if (c.create < 5 || c.orphan < 5) return '';
    return `
        <div class="cm-adopt-banner"
             style="margin-bottom:1em;padding:0.85em 1em;
                    background:#f6ffed;border:1px solid #b7eb8f;
                    border-radius:6px">
            <div style="display:flex;justify-content:space-between;align-items:center;gap:1em;flex-wrap:wrap">
                <div>
                    <div style="font-weight:600;color:#389e0d;display:flex;align-items:center;gap:6px">
                        <span style="display:inline-flex;align-items:center">${_icon('zap', 'sm')}</span>
                        <span>${__('Sieht nach erstem Sync aus')}</span>
                    </div>
                    <div class="small text-muted" style="margin-top:0.25em">
                        ${__('{0} planned creates and {1} orphan live categories. ' +
                              'Falls die Namen in beiden Bäumen schon übereinstimmen, kannst du sie ' +
                              'per Pfad zuordnen — danach bleiben fast nur noch Updates übrig.',
                              [c.create, c.orphan])}
                    </div>
                </div>
                <button class="btn btn-sm btn-success cm-adopt-by-path">
                    ${__('Per Pfad zuordnen…')}
                </button>
            </div>
        </div>`;
}


function _render_action_banners(plan, c) {
    const banners = [];
    if (c.drift > 0) {
        banners.push(`
            <div style="margin-bottom:0.75em;padding:0.6em 0.85em;
                        background:#fff1f0;border:1px solid #ffa39e;
                        border-radius:6px;display:flex;align-items:center;gap:8px">
                <span style="color:#cf1322;display:inline-flex;align-items:center">${_icon('triangle-alert', 'sm')}</span>
                <strong>${__('{0} stale Zuordnungen', [c.drift])}</strong>
                <span class="text-muted small">— ${__('point at deleted backend categories. See section below.')}</span>
            </div>`);
    }
    return banners.join('');
}


function _render_metrics_bar(c) {
    // Each metric cell uses a Lucide icon (color inherits via
    // currentColor from the cell's text color, so the muted ones stay
    // subtle and the action-colored ones pop where we tint).
    const cells = [
        { color: '#52c41a', icon: _icon('plus', 'sm'),           label: __('Anlegen'),                value: c.create },
        { color: '#fa8c16', icon: _icon('pencil', 'sm'),         label: __('Aktualisieren'),          value: c.update },
        { color: '#faad14', icon: _icon('move', 'sm'),           label: __('Verschieben'),            value: c.move },
        { color: 'inherit', icon: _icon('minus', 'sm'),          label: __('No change'),         value: c.noop },
        { color: '#cf1322', icon: _icon('circle-slash', 'sm'),   label: __('Verwaiste'),              value: c.orphan },
        { color: '#cf1322', icon: _icon('unlink', 'sm'),         label: __('Mapping moved'), value: c.drift },
    ];
    const html = cells.map((cell) => `
        <div style="flex:1;min-width:110px;padding:0.5em 0.75em;
                    background:white;border:1px solid var(--border-color);
                    border-radius:4px;text-align:center">
            <div style="font-size:18px;font-weight:600">${cell.value}</div>
            <div class="text-muted small" style="margin-top:2px;display:flex;align-items:center;justify-content:center;gap:4px">
                <span style="color:${cell.color}">${cell.icon}</span>${cell.label}
            </div>
        </div>`).join('');
    return `
        <div style="display:flex;gap:0.5em;margin-bottom:1em;flex-wrap:wrap">
            ${html}
        </div>`;
}


function _render_notes(plan) {
    if (!plan.notes || !plan.notes.length) return '';
    const items = plan.notes.map((n) =>
        `<li style="margin:0.2em 0">${frappe.utils.escape_html(n)}</li>`,
    ).join('');
    return `
        <div style="margin-bottom:0.75em;padding:0.6em 0.85em;
                    background:#fffbe6;border:1px solid #ffe58f;
                    border-radius:6px">
            <ul style="margin:0;padding-left:1.25em">${items}</ul>
        </div>`;
}


// Baum-Diff as a compact, filterable, searchable data table.
// Path-as-breadcrumb makes the hierarchy readable without nesting; the
// real tree shape is implicit in the alphabetical-by-path sort. We
// dropped the bullet-list rendering because at 200+ nodes the indented
// outline became wall-of-text.
function _render_tree_section(plan) {
    const all_nodes = [
        ...(plan.creates || []), ...(plan.updates || []),
        ...(plan.moves || []), ...(plan.noops || []),
    ];
    if (!all_nodes.length) {
        return `<div class="text-muted">${__('Keine Baum-Knoten im Plan.')}</div>`;
    }
    // Stamp the action onto each node once so the row renderer doesn't
    // need a second lookup.
    for (const bucket of ['creates', 'updates', 'moves', 'noops']) {
        const single = bucket.slice(0, -1);
        for (const n of plan[bucket] || []) n._action = single;
    }
    const sorted = all_nodes.slice().sort((a, b) => {
        const pa = (a.item_group_path || a.item_group || '').toLowerCase();
        const pb = (b.item_group_path || b.item_group || '').toLowerCase();
        return pa.localeCompare(pb);
    });

    const c = _bucket_counts(plan);
    const rows = sorted.map(_render_tree_row).join('');
    return `
        <div class="cm-section" style="margin-bottom:1em">
            <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:0.5em">
                <h6 style="margin:0">${__('Baum-Diff')}
                    <span class="text-muted small">(${all_nodes.length} ${__('Knoten')})</span>
                </h6>
                <input type="search" class="form-control cm-tree-search"
                       placeholder="${__('Pfad oder Name suchen…')}"
                       style="max-width:240px;height:28px;font-size:12px">
            </div>
            <div class="cm-filter-chips" style="display:flex;gap:0.35em;flex-wrap:wrap;margin:0.5em 0">
                ${_render_filter_chip('all',    _icon('list', 'xs') + ' ' + __('Alle'),         c.create + c.update + c.move + c.noop, true)}
                ${_render_filter_chip('create', _icon('plus', 'xs') + ' ' + __('Anlegen'),       c.create, false)}
                ${_render_filter_chip('update', _icon('pencil', 'xs') + ' ' + __('Aktualisieren'), c.update, false)}
                ${_render_filter_chip('move',   _icon('move', 'xs') + ' ' + __('Verschieben'),   c.move,   false)}
                ${_render_filter_chip('noop',   _icon('minus', 'xs') + ' ' + __('No change'), c.noop,  false)}
            </div>
            <div style="max-height:480px;overflow:auto;
                        border:1px solid var(--border-color);border-radius:4px">
                <table class="table table-sm cm-tree-table"
                       style="margin:0;font-size:12px;table-layout:fixed;width:100%">
                    <colgroup>
                        <col style="width:120px"><col><col style="width:35%"><col style="width:110px">
                    </colgroup>
                    <thead style="position:sticky;top:0;background:var(--bg-light, #fafbfc);z-index:1">
                        <tr>
                            <th>${__('Aktion')}</th>
                            <th>${__('Pfad')}</th>
                            <th>${__('Detail')}</th>
                            <th style="text-align:right"></th>
                        </tr>
                    </thead>
                    <tbody>${rows}</tbody>
                </table>
            </div>
        </div>`;
}


function _render_filter_chip(key, label, value, active) {
    return `<button class="btn btn-xs cm-tree-filter ${active ? 'btn-primary' : 'btn-default'}"
                    data-filter="${key}" style="font-size:11px">
        ${label} <span class="badge" style="margin-left:0.3em;background:rgba(0,0,0,.15)">${value}</span>
    </button>`;
}


function _render_tree_row(node) {
    const action = node._action || 'noop';
    const v = _action_visuals(action);
    const path_full = node.item_group_path || node.item_group || '';
    const parts = path_full.split(' / ');
    const last = parts.pop() || '';
    const prefix = parts.length ? parts.join(' / ') + ' / ' : '';

    // Build detail column based on action type — compact strings, no
    // long UUID display (truncate to 12 chars with full value on hover).
    let detail = '';
    let action_cell = '';
    if (action === 'create') {
        const ext = node.proposed_parent_external_id || '';
        const parent_html = ext
            ? `<code title="${frappe.utils.escape_html(ext)}">${_short_uuid(ext)}</code>`
            : `<em class="text-muted">${__('Wurzel')}</em>`;
        detail = `<span class="text-muted">${__('unter')} ${parent_html}</span>`;
        const ig = frappe.utils.escape_html(node.item_group);
        action_cell = `<button class="btn btn-xs btn-default cm-adopt-create"
            data-ig="${ig}"
            data-parent-ext="${frappe.utils.escape_html(node.proposed_parent_external_id || '')}">
            ${__('Adoptieren…')}
        </button>`;
    } else if (action === 'update') {
        const diffs = [];
        if (node.current_name && node.current_name !== node.proposed_name) {
            diffs.push(`<span title="${frappe.utils.escape_html(node.current_name)} → ${frappe.utils.escape_html(node.proposed_name)}">${__('Name')}</span>`);
        }
        if ((node.current_description || '') !== (node.proposed_description || '')) {
            diffs.push(__('Description'));
        }
        if (node.current_active !== node.proposed_active) {
            diffs.push(__('aktiv-Flag'));
        }
        detail = `<span class="text-muted">${diffs.join(', ')}</span>`;
    } else if (action === 'move') {
        const old_p = node.current_parent_external_id;
        const new_p = node.proposed_parent_external_id;
        const old_html = old_p
            ? `<code title="${frappe.utils.escape_html(old_p)}">${_short_uuid(old_p)}</code>`
            : `<em>${__('Wurzel')}</em>`;
        const new_html = new_p
            ? `<code title="${frappe.utils.escape_html(new_p)}">${_short_uuid(new_p)}</code>`
            : `<em>${__('Wurzel')}</em>`;
        detail = `<span class="text-muted">${old_html} → ${new_html}</span>`;
    } else {
        detail = `<span class="text-muted">—</span>`;
    }

    const notes = (node.notes || [])
        .map((n) => ` <span class="text-warning" title="${frappe.utils.escape_html(n)}">${_icon('triangle-alert', 'xs')}</span>`)
        .join('');

    // data-search/data-action used by the filter+search handlers; kept
    // pre-lowercased on the row so input handling is O(rows) and never
    // re-serializes the node.
    const haystack = (path_full + ' ' + (node.proposed_name || '')).toLowerCase();
    return `<tr class="cm-tree-row" data-action="${action}"
                data-search="${frappe.utils.escape_html(haystack)}">
        <td><span class="indicator-pill ${v.color}" style="white-space:nowrap">${v.label}</span></td>
        <td style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap"
            title="${frappe.utils.escape_html(path_full)}">
            <span class="text-muted">${frappe.utils.escape_html(prefix)}</span><strong>${frappe.utils.escape_html(last)}</strong>${notes}
        </td>
        <td style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${detail}</td>
        <td style="text-align:right">${action_cell}</td>
    </tr>`;
}


function _short_uuid(s) {
    if (!s) return '';
    return s.length > 12 ? s.slice(0, 10) + '…' : s;
}


function _action_visuals(action) {
    // No per-row SVG: with 200+ tree rows the SVG-<use> resolutions
    // visibly drag layout. The colored pill already carries the
    // action signal — Lucide icons stay in the headers/metrics/buttons
    // where they're <20 in total.
    const map = {
        create: { color: 'green',  label: __('Anlegen') },
        update: { color: 'orange', label: __('Update') },
        move:   { color: 'yellow', label: __('Verschieben') },
        noop:   { color: 'gray',   label: __('No change') },
        error:  { color: 'red',    label: __('Fehler') },
    };
    return map[action] || { color: 'gray', label: (action || '').toUpperCase() };
}


// Orphans as a sortable, searchable table with multi-select + bulk
// actions. Single-row Adopt/Delete/Keep stays per-row for one-offs;
// bulk Delete/Keep handles "delete the entire dead branch" cleanly.
function _render_orphans_section(plan, orphan_policy) {
    const orphans = plan.orphans || [];
    if (!orphans.length) return '';
    const rows = orphans.map(_render_orphan_row).join('');
    return `
        <div class="cm-section" style="margin-top:1em">
            <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:0.5em">
                <h6 style="margin:0;display:flex;align-items:center;gap:6px">
                    <span style="color:#cf1322;display:inline-flex;align-items:center">${_icon('circle-slash', 'sm')}</span>
                    <span>${__('Backend categories without ERPNext counterpart')}</span>
                    <span class="text-muted small">(${orphans.length} · ${__('Policy')}: ${frappe.utils.escape_html(orphan_policy || 'keep')})</span>
                </h6>
                <input type="search" class="form-control cm-orphans-search"
                       placeholder="${__('Pfad oder ID suchen…')}"
                       style="max-width:240px;height:28px;font-size:12px">
            </div>
            <div style="display:flex;gap:0.35em;margin:0.5em 0;align-items:center">
                <span class="text-muted small cm-orphans-selcount">${__('0 selected')}</span>
                <div style="flex:1"></div>
                <button class="btn btn-xs btn-default cm-orphans-bulk-keep" disabled>${__('Keep selection')}</button>
                <button class="btn btn-xs btn-danger cm-orphans-bulk-delete" disabled>${__('Delete selection')}</button>
            </div>
            <div style="max-height:380px;overflow:auto;
                        border:1px solid var(--border-color);border-radius:4px">
                <table class="table table-sm cm-orphans-table"
                       style="margin:0;font-size:12px;table-layout:fixed;width:100%">
                    <colgroup>
                        <col style="width:32px"><col><col style="width:110px"><col style="width:70px"><col style="width:120px">
                    </colgroup>
                    <thead style="position:sticky;top:0;background:var(--bg-light, #fafbfc);z-index:1">
                        <tr>
                            <th><input type="checkbox" class="cm-orphan-cb-all" title="${__('Alle')}"></th>
                            <th>${__('Pfad')}</th>
                            <th>${__('Backend-ID')}</th>
                            <th class="text-right">${__('Produkte')}</th>
                            <th></th>
                        </tr>
                    </thead>
                    <tbody>${rows}</tbody>
                </table>
            </div>
        </div>`;
}


function _render_orphan_row(o) {
    const ext = frappe.utils.escape_html(o.external_id);
    const name = frappe.utils.escape_html(o.name || '');
    const path = o.path || o.name || '';
    const haystack = (path + ' ' + o.external_id).toLowerCase();
    return `<tr data-search="${frappe.utils.escape_html(haystack)}">
        <td><input type="checkbox" class="cm-orphan-cb" data-ext="${ext}"></td>
        <td style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap"
            title="${frappe.utils.escape_html(path)}">${frappe.utils.escape_html(path)}</td>
        <td><code title="${ext}" style="font-size:11px">${_short_uuid(o.external_id)}</code></td>
        <td class="text-right">${o.product_count || 0}</td>
        <td style="text-align:right;white-space:nowrap">
            <button class="btn btn-xs btn-default cm-orphan-adopt"
                    data-ext="${ext}" data-name="${name}"
                    title="${__('Link with item group')}">${_ci('link')}</button>
            <button class="btn btn-xs btn-danger cm-orphan-delete"
                    data-ext="${ext}"
                    title="${__('Delete backend category')}">${_ci('trash')}</button>
            <button class="btn btn-xs btn-default cm-orphan-keep"
                    data-ext="${ext}"
                    title="${__('Behalten (Standard)')}">${_ci('check')}</button>
        </td>
    </tr>`;
}


function _render_drift_section(plan) {
    const drift = plan.mapping_drift || [];
    if (!drift.length) return '';
    const rows = drift.map((d) => {
        const ig = frappe.utils.escape_html(d.item_group);
        const ext = frappe.utils.escape_html(d.stale_external_id);
        return `<tr>
            <td>${ig}</td>
            <td><code title="${ext}" style="font-size:11px">${_short_uuid(d.stale_external_id)}</code></td>
            <td>${frappe.utils.escape_html(d.proposed_name || '')}</td>
            <td style="text-align:right">
                <button class="btn btn-xs btn-default cm-drift-clear" data-ig="${ig}">
                    ${__('Clear mapping and re-create')}
                </button>
            </td>
        </tr>`;
    }).join('');
    return `
        <div class="cm-section" style="margin-top:1em">
            <h6 style="margin:0;display:flex;align-items:center;gap:6px">
                <span style="color:#cf1322;display:inline-flex;align-items:center">${_icon('triangle-alert', 'sm')}</span>
                <span>${__('Verlorene Zuordnungen')}</span>
                <span class="text-muted small">(${drift.length})</span>
            </h6>
            <div class="text-muted small" style="margin:0.25em 0">
                ${__('Item groups whose stored backend category no longer exists in the live tree.')}
            </div>
            <div style="max-height:280px;overflow:auto;
                        border:1px solid var(--border-color);border-radius:4px;margin-top:0.5em">
                <table class="table table-sm" style="margin:0;font-size:12px;table-layout:fixed;width:100%">
                    <colgroup><col><col style="width:120px"><col><col style="width:200px"></colgroup>
                    <thead style="position:sticky;top:0;background:var(--bg-light, #fafbfc);z-index:1">
                        <tr>
                            <th>${__('Artikelgruppe')}</th>
                            <th>${__('Veraltete ID')}</th>
                            <th>${__('Vorgeschlagener Name')}</th>
                            <th></th>
                        </tr>
                    </thead>
                    <tbody>${rows}</tbody>
                </table>
            </div>
        </div>`;
}


// ── Adopt sub-dialogs ──────────────────────────────────────────────

// We don't expose `find_matching_nodes` as a whitelisted endpoint yet
// (Phase 5 owns that). For CREATE the operator pastes the external_id
// they read from the backend admin's URL bar; for orphans they pick a
// target Item Group via the standard Link autocomplete.

function _open_adopt_dialog_for_create(frm, parent_dialog, item_group, parent_ext) {
    const sub = new frappe.ui.Dialog({
        title: __('Adopt existing…') + ' — ' + item_group,
        fields: [
            { fieldname: 'info', fieldtype: 'HTML',
              options: `<div class="text-muted small">${__('Paste the backend category ID for this Item Group. The mapping is stored as a node_override on the mirror so it survives re-walks.')}</div>` },
            { fieldname: 'external_id', fieldtype: 'Data', label: __('External ID'), reqd: 1,
              description: parent_ext ? __('Suggested parent: {0}', [parent_ext]) : '' },
            { fieldname: 'mode', fieldtype: 'Select', label: __('Mode'),
              options: 'pin\nskip', default: 'pin',
              description: __("'pin' wires the IG to this external_id. 'skip' tells the mirror to ignore this IG.") },
        ],
        primary_action_label: __('Adopt'),
        primary_action(values) {
            _call_adopt_node(frm, parent_dialog, sub, item_group,
                values.external_id || '', values.mode || 'pin');
        },
    });
    sub.show();
}


function _open_adopt_dialog_for_orphan(frm, parent_dialog, external_id, name) {
    const sub = new frappe.ui.Dialog({
        title: __('Adopt → IG…') + ' — ' + (name || external_id),
        fields: [
            { fieldname: 'info', fieldtype: 'HTML',
              options: `<div class="text-muted small">${__('Pick the ERPNext Item Group that should own this backend category.')}</div>` },
            { fieldname: 'item_group', fieldtype: 'Link', label: __('Item Group'),
              options: 'Item Group', reqd: 1 },
            { fieldname: 'external_id_display', fieldtype: 'Data', label: __('External ID'),
              default: external_id, read_only: 1 },
        ],
        primary_action_label: __('Adopt'),
        primary_action(values) {
            if (!values.item_group) return;
            _call_adopt_node(frm, parent_dialog, sub, values.item_group, external_id, 'pin');
        },
    });
    sub.show();
}


function _call_adopt_node(frm, parent_dialog, sub_dialog, item_group, external_id, mode) {
    frappe.call({
        method: 'ecommerce_integrations.catalog_mirror.api.adopt_node',
        args: { mirror: frm.doc.name, item_group, external_id, mode },
        freeze: true,
        freeze_message: __('Adopting…'),
        callback() {
            sub_dialog.hide();
            frappe.show_alert({
                message: __('Adopted: {0}', [item_group]),
                indicator: 'green',
            });
            _refresh_preview_inplace(frm, parent_dialog);
        },
    });
}


function _orphan_action(frm, parent_dialog, external_id, action) {
    frappe.call({
        method: 'ecommerce_integrations.catalog_mirror.api.set_orphan_action',
        args: { mirror: frm.doc.name, external_id, action },
        freeze: true,
        freeze_message: __('Updating orphan…'),
        callback(r) {
            if (!r.message) return;
            frappe.show_alert({
                message: __('Orphan {0}: {1}', [external_id, action]),
                indicator: action === 'delete' ? 'red' : 'blue',
            });
            _refresh_preview_inplace(frm, parent_dialog);
        },
    });
}


// "Clear mapping" writes external_id='' via adopt_node mode='skip' —
// the orchestrator persists that by clearing the IG's <backend>_category_id,
// which is exactly "treat as a new create next sync".
function _clear_drift_mapping(frm, parent_dialog, item_group) {
    frappe.confirm(
        __('Clear backend mapping on Item Group {0}? The next sync will re-create the category.', [item_group]),
        () => frappe.call({
            method: 'ecommerce_integrations.catalog_mirror.api.adopt_node',
            args: { mirror: frm.doc.name, item_group, external_id: '', mode: 'skip' },
            freeze: true,
            freeze_message: __('Clearing mapping…'),
            callback() {
                frappe.show_alert({
                    message: __('Cleared: {0}', [item_group]),
                    indicator: 'orange',
                });
                _refresh_preview_inplace(frm, parent_dialog);
            },
        }),
    );
}


function _refresh_preview_inplace(frm, dialog) {
    if (!dialog || !dialog.fields_dict || !dialog.fields_dict.preview_html) return;
    frappe.call({
        method: 'ecommerce_integrations.catalog_mirror.api.preview_mirror',
        args: { mirror: frm.doc.name },
        callback(r) {
            if (!r.message) return;
            dialog._plan = r.message;
            dialog.fields_dict.preview_html.$wrapper.html(_render_preview(r.message, frm));
        },
    });
}
