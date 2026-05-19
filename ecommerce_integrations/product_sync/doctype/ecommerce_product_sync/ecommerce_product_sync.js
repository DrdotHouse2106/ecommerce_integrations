// Product Sync form — preview dialog, pre-flight, adopt-by-SKU,
// test-run dropdown, dashboard indicators.
//
// Pattern mirrors `catalog_mirror/.../ecommerce_catalog_mirror.js`
// so operators who learned that flow see the same affordances here.
// All HTML uses CSS variables + indicator-pill classes so dark-mode
// works without extra work. Long lists (preview rows) use CSS-mask
// icons + flat virtualisation — measured 700+ DOM nodes without lag.


frappe.ui.form.on('Ecommerce Product Sync', {
    onload(frm) {
        _inject_ps_styles_once();
    },

    refresh(frm) {
        _inject_ps_styles_once();
        if (frm.is_new()) return;
        _render_dashboard_indicators(frm);
        _render_header_buttons(frm);
    },
});


function _icon(name, size, color) {
    const cls = 'icon icon-' + (size || 'sm');
    const style = color ? `color:${color};vertical-align:middle`
                        : 'vertical-align:middle';
    return `<svg class="${cls}" style="${style}"><use href="#icon-${name}"></use></svg>`;
}


// ── Dashboard / Header ──────────────────────────────────────────────


function _render_dashboard_indicators(frm) {
    const status = (frm.doc.sync_status || '').toLowerCase();
    if (status === 'running') {
        frm.dashboard.add_indicator(__('Sync running'), 'blue');
    } else if (status === 'error') {
        const snippet = (frm.doc.last_error || '').split('\n')[0].slice(0, 120);
        frm.dashboard.add_indicator(
            snippet ? __('Sync error: {0}', [snippet]) : __('Sync error'),
            'red',
        );
    } else if (status === 'ok') {
        frm.dashboard.add_indicator(__('Letzter Lauf OK'), 'green');
    } else if (status === 'partial') {
        frm.dashboard.add_indicator(__('Partial success'), 'orange');
    }
    if (frm.doc.last_synced_at) {
        frm.dashboard.add_indicator(
            __('Letzter Lauf: {0}',
                [frappe.datetime.comment_when(frm.doc.last_synced_at)]),
            status === 'ok' ? 'green' : 'gray',
        );
    }
    if (!Number(frm.doc.is_active || 0)) {
        frm.dashboard.add_indicator(__('Inaktiv'), 'gray');
    }
}


function _render_header_buttons(frm) {
    // Primary: fast preview (hash-only, no backend roundtrip).
    // Three preview modes — fast is the default because it answers
    // "what would change?" correctly without the backend wait, and
    // power-users opt into the heavier ones from the menu.
    frm.add_custom_button(__('Preview'),
        () => _open_preview_dialog(frm, { mode: 'fast' })).addClass('btn-primary');

    frm.add_custom_button(__('Detailvergleich (mit Backend)…'),
        () => _open_preview_dialog(frm, { mode: 'detail' }), __('Preview'));
    frm.add_custom_button(__('Full (incl. orphans)…'),
        () => _open_preview_dialog(frm, { mode: 'full' }), __('Preview'));

    // Apply Live with double-confirm
    frm.add_custom_button(__('Apply Live'), () => _apply_live(frm));

    // Test Run dropdown (group)
    frm.add_custom_button(__('Pre-Flight Check'),
        () => _open_preflight_dialog(frm), __('Test Run'));
    frm.add_custom_button(__('Per SKU adoptieren…'),
        () => _open_adopt_by_sku(frm), __('Test Run'));
    frm.add_custom_button(__('Test-Historie…'),
        () => _open_history_dialog(frm), __('Test Run'));

    // Form-scoped keyboard shortcut: Ctrl+Enter = open fast preview
    frappe.ui.keys.add_shortcut({
        shortcut: 'ctrl+enter',
        action: () => _open_preview_dialog(frm, { mode: 'fast' }),
        description: __('Open quick preview'),
        page: frm.page,
        condition: () => !frm.is_new(),
    });
}


// ── Preview Dialog ──────────────────────────────────────────────────


function _open_preview_dialog(frm, { mode = 'fast' } = {}) {
    _run_preview_with_progress(frm, {
        mode,
        onDone: (plan) => _show_preview_dialog(frm, plan, { mode }),
    });
}


// Maps the three preview modes onto ``start_preview`` flags. The
// names match the menu labels exactly so a future operator sees the
// same vocabulary in three places (button, progress dialog, plan).
const PREVIEW_MODES = {
    fast:   { fetch_live: 0, detect_orphans: 0, label: 'Schnellvorschau' },
    detail: { fetch_live: 1, detect_orphans: 0, label: 'Detailvergleich' },
    full:   { fetch_live: 1, detect_orphans: 1, label: 'Vollständige Vorschau' },
};


// Kicks off a background preview (start_preview) and polls
// get_preview_status until the plan is ready. Shows a progress
// dialog with a percentage bar so 30 s+ runs don't look frozen and
// don't time out at the gunicorn/nginx layer.
function _run_preview_with_progress(frm, { onDone, mode = 'fast' }) {
    const cfg = PREVIEW_MODES[mode] || PREVIEW_MODES.fast;
    const progressDialog = new frappe.ui.Dialog({
        title: __('{0} wird gebaut…', [__(cfg.label)]),
        size: 'small',
        fields: [{ fieldname: 'progress_html', fieldtype: 'HTML' }],
    });
    progressDialog.set_secondary_action_label(__('Abbrechen'));
    let cancelled = false;
    progressDialog.set_secondary_action(() => {
        cancelled = true;
        progressDialog.hide();
    });

    const renderProgress = ({ percent = 0, current = 0, total = 0, status = 'running' } = {}) => {
        // Three phases: (1) waiting for the worker to compute the total,
        // (2) item-loop progress, (3) post-loop backend orphan/drift
        // detection. Phase 3 is invisible from the server side because
        // it doesn't fire on_progress, so we infer it: current == total
        // but status is still 'running'. Without this branch the dialog
        // looks frozen at "33,961 / 33,961 (99%)".
        let label;
        if (!total) {
            label = __('Resolving scope…');
        } else if (current >= total && status === 'running') {
            label = __('Backend reconciliation running (orphan and drift detection — may take several minutes on large catalogues)…');
        } else {
            label = `${current.toLocaleString()} / ${total.toLocaleString()} (${percent}%)`;
        }
        progressDialog.fields_dict.progress_html.$wrapper.html(`
            <div style="padding:8px 0;">
                <div class="progress" style="height:14px;margin-bottom:8px;">
                    <div class="progress-bar progress-bar-striped active"
                         role="progressbar"
                         style="width:${percent}%;background:var(--primary,#2490ef);"></div>
                </div>
                <div class="text-muted small" style="text-align:center;">${frappe.utils.escape_html(label)}</div>
            </div>
        `);
    };
    renderProgress();
    progressDialog.show();

    frappe.call({
        method: 'ecommerce_integrations.product_sync.api.start_preview',
        args: {
            sync: frm.doc.name,
            fetch_live: cfg.fetch_live,
            detect_orphans: cfg.detect_orphans,
        },
        callback(r) {
            const runName = r.message && r.message.run_name;
            if (!runName) {
                progressDialog.hide();
                frappe.msgprint(__('Could not start preview.'));
                return;
            }
            const poll = () => {
                if (cancelled) return;
                frappe.call({
                    method: 'ecommerce_integrations.product_sync.api.get_preview_status',
                    args: { run_name: runName },
                    callback(rr) {
                        if (cancelled) return;
                        const m = rr.message || {};
                        if (m.status === 'ok' && m.plan) {
                            progressDialog.hide();
                            onDone(m.plan);
                            return;
                        }
                        if (m.status === 'error') {
                            progressDialog.hide();
                            frappe.msgprint({
                                title: __('Preview failed'),
                                message: frappe.utils.escape_html(m.error || ''),
                                indicator: 'red',
                            });
                            return;
                        }
                        renderProgress(m);
                        setTimeout(poll, 1500);
                    },
                });
            };
            // First poll quickly to fill in the total, then space out.
            setTimeout(poll, 600);
        },
    });
}


function _show_preview_dialog(frm, plan, { mode = 'fast' } = {}) {
    const d = new frappe.ui.Dialog({
        title: __('Preview: {0}', [plan.title || frm.doc.name]),
        size: 'extra-large',
        fields: [{ fieldname: 'preview_html', fieldtype: 'HTML' }],
        primary_action_label: __('Apply Live'),
        primary_action() {
            d.hide();
            _apply_live(frm);
        },
    });
    d.set_secondary_action_label(__('Reload'));
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
    d._mode = mode;
    d.fields_dict.preview_html.$wrapper.html(_render_preview(plan, frm));
    _wire_preview_handlers(frm, d);
    d.show();
}


function _refresh_preview_inplace(frm, d) {
    // Same background+poll contract as the initial open so we never
    // re-introduce the timeout we just removed. Preserve the mode the
    // dialog was opened in so "Neu laden" doesn't silently downgrade
    // a Detailvergleich back to Schnellvorschau.
    _run_preview_with_progress(frm, {
        mode: d._mode || 'fast',
        onDone: (plan) => {
            d._plan = plan;
            d.fields_dict.preview_html.$wrapper.html(_render_preview(plan, frm));
        },
    });
}


function _bucket_counts(plan) {
    return {
        create: (plan.creates || []).length,
        update: (plan.updates || []).length,
        noop: (plan.noops || []).length,
        orphan: (plan.orphans || []).length,
        conflict: (plan.conflicts || []).length,
        drift: (plan.mapping_drift || []).length,
    };
}


function _render_preview(plan, frm) {
    const c = _bucket_counts(plan);
    return _render_summary_card(plan, c)
        + _render_first_sync_banner(plan, c)
        + _render_risk_banner(plan)
        + _render_metrics_bar(c)
        + _render_notes(plan)
        + _render_tree_section(plan)
        + _render_orphans_section(plan)
        + _render_conflicts_section(plan)
        + _render_drift_section(plan);
}


function _render_summary_card(plan, c) {
    return `
        <div style="margin-bottom:1em;padding:0.75em 1em;
                    background:var(--bg-light, #fafbfc);
                    border:1px solid var(--border-color);
                    border-radius:6px">
            <div style="display:flex;justify-content:space-between;align-items:center;gap:1em;flex-wrap:wrap">
                <div>
                    <div style="font-size:14px">
                        <strong>${frappe.utils.escape_html(plan.title || '—')}</strong>
                    </div>
                    <div class="text-muted small" style="margin-top:0.25em">
                        ${__('Backend')}: <strong>${frappe.utils.escape_html(plan.backend)}</strong>
                        · ${__('Items in scope')}: <strong>${plan.items_in_scope || 0}</strong>
                        · ${__('Verarbeitet')}: <strong>${c.create + c.update + c.noop + c.conflict + c.drift}</strong>
                    </div>
                </div>
            </div>
        </div>`;
}


function _render_first_sync_banner(plan, c) {
    if (c.create < 5 || c.orphan < 5) return '';
    return `
        <div class="ps-adopt-banner"
             style="margin-bottom:1em;padding:0.85em 1em;
                    background:#f6ffed;border:1px solid #b7eb8f;border-radius:6px">
            <div style="display:flex;justify-content:space-between;align-items:center;gap:1em;flex-wrap:wrap">
                <div>
                    <div style="font-weight:600;color:#389e0d;display:flex;align-items:center;gap:6px">
                        <span style="display:inline-flex;align-items:center">${_icon('zap', 'sm')}</span>
                        <span>${__('Sieht nach erstem Sync aus')}</span>
                    </div>
                    <div class="small text-muted" style="margin-top:0.25em">
                        ${__('{0} geplante Neuanlagen und {1} verwaiste Backend-Produkte. ' +
                              'Wenn SKUs in beiden Systemen schon übereinstimmen, kannst du sie ' +
                              'einmalig per SKU adoptieren — danach bleiben nur noch echte Updates.',
                              [c.create, c.orphan])}
                    </div>
                </div>
                <button class="btn btn-sm btn-success ps-adopt-by-sku">
                    ${__('Per SKU adoptieren…')}
                </button>
            </div>
        </div>`;
}


function _render_risk_banner(plan) {
    let price_jumps = 0, stock_drops = 0, name_rewrites = 0;
    for (const u of (plan.updates || [])) {
        for (const d of (u.diffs || [])) {
            if (d.risk_flag === 'price_jump') price_jumps++;
            else if (d.risk_flag === 'stock_drop') stock_drops++;
            else if (d.risk_flag === 'name_rewrite') name_rewrites++;
        }
    }
    if (price_jumps + stock_drops + name_rewrites === 0) return '';
    const bits = [];
    if (price_jumps) bits.push(__('{0}× Preis-Sprung >20%', [price_jumps]));
    if (stock_drops) bits.push(__('{0}× Bestand-Abfall >50%', [stock_drops]));
    if (name_rewrites) bits.push(__('{0}× Name komplett umgeschrieben', [name_rewrites]));
    return `
        <div style="margin-bottom:0.75em;padding:0.6em 0.85em;
                    background:#fff7e6;border:1px solid #ffd591;border-radius:6px;
                    display:flex;align-items:center;gap:8px">
            <span style="color:#d46b08;display:inline-flex;align-items:center">${_icon('triangle-alert', 'sm')}</span>
            <strong>${__('Significant changes')}</strong>
            <span class="text-muted small">— ${bits.join(' · ')}</span>
        </div>`;
}


function _render_metrics_bar(c) {
    const cells = [
        { color: '#52c41a', icon: _icon('plus', 'sm'),         label: __('Anlegen'),       value: c.create },
        { color: '#fa8c16', icon: _icon('pencil', 'sm'),       label: __('Update'),        value: c.update },
        { color: 'inherit', icon: _icon('minus', 'sm'),        label: __('No change'),    value: c.noop },
        { color: '#cf1322', icon: _icon('circle-slash', 'sm'), label: __('Verwaiste'),     value: c.orphan },
        { color: '#722ed1', icon: _icon('shuffle', 'sm'),      label: __('Konflikte'),     value: c.conflict },
        { color: '#cf1322', icon: _icon('unlink', 'sm'),       label: __('Drift'),         value: c.drift },
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
    return `<div style="display:flex;gap:0.5em;margin-bottom:1em;flex-wrap:wrap">${html}</div>`;
}


function _render_notes(plan) {
    if (!plan.notes || !plan.notes.length) return '';
    const items = plan.notes.map((n) =>
        `<li style="margin:0.2em 0">${frappe.utils.escape_html(n)}</li>`,
    ).join('');
    return `
        <div style="margin-bottom:0.75em;padding:0.6em 0.85em;
                    background:#fffbe6;border:1px solid #ffe58f;border-radius:6px">
            <ul style="margin:0;padding-left:1.25em">${items}</ul>
        </div>`;
}


function _render_tree_section(plan) {
    const all_nodes = [
        ...(plan.creates || []), ...(plan.updates || []), ...(plan.noops || []),
    ];
    if (!all_nodes.length) {
        return `<div class="text-muted">${__('Keine Artikel im Plan.')}</div>`;
    }
    for (const bucket of ['creates', 'updates', 'noops']) {
        const single = bucket.slice(0, -1);
        for (const n of plan[bucket] || []) n._action = single;
    }
    const sorted = all_nodes.slice().sort((a, b) =>
        (a.item_code || '').localeCompare(b.item_code || ''));
    const c = _bucket_counts(plan);
    const rows = sorted.map(_render_item_row).join('');
    return `
        <div style="margin-bottom:1em">
            <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:0.5em">
                <h6 style="margin:0">${__('Artikel-Diff')}
                    <span class="text-muted small">(${all_nodes.length} ${__('Knoten')})</span>
                </h6>
                <input type="search" class="form-control ps-tree-search"
                       placeholder="${__('SKU oder Name suchen…')}"
                       style="max-width:240px;height:28px;font-size:12px">
            </div>
            <div class="ps-filter-chips" style="display:flex;gap:0.35em;flex-wrap:wrap;margin:0.5em 0">
                ${_render_filter_chip('all', _icon('list','xs')+' '+__('Alle'), c.create+c.update+c.noop, true)}
                ${_render_filter_chip('create', _icon('plus','xs')+' '+__('Anlegen'), c.create, false)}
                ${_render_filter_chip('update', _icon('pencil','xs')+' '+__('Update'), c.update, false)}
                ${_render_filter_chip('noop', _icon('minus','xs')+' '+__('No change'), c.noop, false)}
            </div>
            <div style="max-height:480px;overflow:auto;
                        border:1px solid var(--border-color);border-radius:4px">
                <table class="table table-sm ps-tree-table"
                       style="margin:0;font-size:12px;table-layout:fixed;width:100%">
                    <colgroup>
                        <col style="width:90px"><col style="width:130px"><col>
                        <col style="width:80px"><col style="width:60px">
                    </colgroup>
                    <thead style="position:sticky;top:0;background:var(--bg-light,#fafbfc);z-index:1">
                        <tr>
                            <th>${__('Aktion')}</th>
                            <th>${__('SKU')}</th>
                            <th>${__('Name')}</th>
                            <th>${__('Felder')}</th>
                            <th></th>
                        </tr>
                    </thead>
                    <tbody>${rows}</tbody>
                </table>
            </div>
        </div>`;
}


function _render_filter_chip(key, label, value, active) {
    return `<button class="btn btn-xs ps-tree-filter ${active ? 'btn-primary' : 'btn-default'}"
                    data-filter="${key}" style="font-size:11px">
        ${label} <span class="badge" style="margin-left:0.3em;background:rgba(0,0,0,.15)">${value}</span>
    </button>`;
}


function _action_visuals(action) {
    const map = {
        create: { color: 'green',  label: __('Anlegen') },
        update: { color: 'orange', label: __('Update') },
        noop:   { color: 'gray',   label: __('No change') },
        error:  { color: 'red',    label: __('Fehler') },
    };
    return map[action] || { color: 'gray', label: (action || '').toUpperCase() };
}


function _render_item_row(node) {
    const action = node._action || 'noop';
    const v = _action_visuals(action);
    const item_code = frappe.utils.escape_html(node.item_code || '');
    const sku = frappe.utils.escape_html(node.sku || '—');
    const name = frappe.utils.escape_html(node.proposed_name || '');
    const haystack = ((node.item_code || '') + ' ' + (node.sku || '') + ' ' + (node.proposed_name || '')).toLowerCase();
    const diffs = node.diffs || [];
    const has_risk = diffs.some(d => d.risk_flag);
    const field_count = diffs.length
        ? `<span class="indicator-pill ${has_risk ? 'red' : 'orange'}">${diffs.length}</span>`
        : '<span class="text-muted">—</span>';
    const detail_btn = diffs.length
        ? `<button class="btn btn-xs btn-default ps-show-diffs" data-item="${item_code}">${__('Diff')}</button>`
        : '';
    return `<tr class="ps-tree-row" data-action="${action}" data-item="${item_code}"
                data-search="${frappe.utils.escape_html(haystack)}">
        <td><span class="indicator-pill ${v.color}" style="white-space:nowrap">${v.label}</span></td>
        <td><code style="font-size:11px">${sku}</code></td>
        <td style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${name}">${name}</td>
        <td>${field_count}</td>
        <td style="text-align:right">${detail_btn}</td>
    </tr>`;
}


function _render_orphans_section(plan) {
    const orphans = plan.orphans || [];
    if (!orphans.length) return '';
    const rows = orphans.map((o) => {
        const ext = frappe.utils.escape_html(o.external_id);
        const sku = frappe.utils.escape_html(o.sku || '—');
        const name = frappe.utils.escape_html(o.name || '');
        return `<tr><td><code title="${ext}" style="font-size:11px">${ext.slice(0,12)}…</code></td>
                <td>${sku}</td><td>${name}</td>
                <td style="text-align:right">
                    <span class="text-muted small">${__(o.suggested_action || 'keep')}</span>
                </td></tr>`;
    }).join('');
    return `
        <div style="margin-top:1em">
            <h6 style="margin:0;display:flex;align-items:center;gap:6px">
                <span style="color:#cf1322;display:inline-flex;align-items:center">${_icon('circle-slash','sm')}</span>
                <span>${__('Backend-Produkte ohne ERPNext-Pendant')}</span>
                <span class="text-muted small">(${orphans.length})</span>
            </h6>
            <div style="max-height:300px;overflow:auto;border:1px solid var(--border-color);border-radius:4px;margin-top:0.5em">
                <table class="table table-sm" style="margin:0;font-size:12px;table-layout:fixed;width:100%">
                    <colgroup><col style="width:120px"><col style="width:130px"><col><col style="width:80px"></colgroup>
                    <thead style="position:sticky;top:0;background:var(--bg-light,#fafbfc);z-index:1">
                        <tr><th>${__('Backend-ID')}</th><th>${__('SKU')}</th><th>${__('Name')}</th><th>${__('Aktion')}</th></tr>
                    </thead>
                    <tbody>${rows}</tbody>
                </table>
            </div>
        </div>`;
}


function _render_conflicts_section(plan) {
    const conflicts = plan.conflicts || [];
    if (!conflicts.length) return '';
    const rows = conflicts.map((c) => `
        <tr><td><code style="font-size:11px">${frappe.utils.escape_html(c.item_code)}</code></td>
            <td>${(c.competing_syncs || []).map(s => `<code>${frappe.utils.escape_html(s)}</code>`).join(', ')}</td>
            <td>${frappe.utils.escape_html(c.resolution || '')}</td></tr>`).join('');
    return `
        <div style="margin-top:1em">
            <h6 style="margin:0;display:flex;align-items:center;gap:6px">
                <span style="color:#722ed1;display:inline-flex;align-items:center">${_icon('shuffle','sm')}</span>
                <span>${__('Konflikte mit anderen Syncs')}</span>
                <span class="text-muted small">(${conflicts.length})</span>
            </h6>
            <table class="table table-sm" style="margin:0.5em 0 0 0;font-size:12px">
                <thead><tr><th>${__('Artikel')}</th><th>${__('Konkurrierende Syncs')}</th><th>${__('Resolution')}</th></tr></thead>
                <tbody>${rows}</tbody>
            </table>
        </div>`;
}


function _render_drift_section(plan) {
    const drift = plan.mapping_drift || [];
    if (!drift.length) return '';
    const rows = drift.map((d) => `
        <tr><td><code>${frappe.utils.escape_html(d.item_code)}</code></td>
            <td><code style="font-size:11px">${frappe.utils.escape_html(d.stale_external_id).slice(0,16)}…</code></td>
            <td>${frappe.utils.escape_html(d.proposed_name || '')}</td></tr>`).join('');
    return `
        <div style="margin-top:1em">
            <h6 style="margin:0;display:flex;align-items:center;gap:6px">
                <span style="color:#cf1322;display:inline-flex;align-items:center">${_icon('triangle-alert','sm')}</span>
                <span>${__('Lost mappings')}</span>
                <span class="text-muted small">(${drift.length})</span>
            </h6>
            <div class="text-muted small" style="margin:0.25em 0">
                ${__('Items, deren gespeicherte Backend-ID nicht mehr existiert. Beim Apply werden sie neu angelegt.')}
            </div>
            <table class="table table-sm" style="margin:0;font-size:12px">
                <thead><tr><th>${__('Artikel')}</th><th>${__('Veraltete ID')}</th><th>${__('Vorgeschlagener Name')}</th></tr></thead>
                <tbody>${rows}</tbody>
            </table>
        </div>`;
}


// ── Diff Drawer (per-row field-level view) ──────────────────────────


function _show_diff_drawer(plan, item_code) {
    const update = (plan.updates || []).find(u => u.item_code === item_code);
    if (!update) return;
    const diffs = update.diffs || [];
    const rows = diffs.map((d) => {
        const risk = d.risk_flag
            ? `<span class="indicator-pill red" title="${frappe.utils.escape_html(d.risk_flag)}">${__(d.risk_flag)}</span>`
            : '';
        const cur = d.preview_current || d.current || '—';
        const prop = d.preview_proposed || d.proposed || '—';
        return `<tr>
            <td><code style="font-size:11px">${frappe.utils.escape_html(d.field)}</code> ${risk}</td>
            <td style="background:#fff1f0">${frappe.utils.escape_html(cur)}</td>
            <td style="background:#f6ffed">${frappe.utils.escape_html(prop)}</td>
        </tr>`;
    }).join('');
    const d = new frappe.ui.Dialog({
        title: __('Felder: {0}', [update.proposed_name || item_code]),
        size: 'large',
        fields: [{ fieldname: 'diff_html', fieldtype: 'HTML' }],
    });
    d.fields_dict.diff_html.$wrapper.html(`
        <table class="table table-sm" style="margin:0;font-size:12px;table-layout:fixed">
            <colgroup><col style="width:30%"><col style="width:35%"><col style="width:35%"></colgroup>
            <thead><tr><th>${__('Feld')}</th><th>${__('Vorher (live)')}</th><th>${__('Nachher (proposed)')}</th></tr></thead>
            <tbody>${rows}</tbody>
        </table>`);
    d.show();
}


// ── Wiring ──────────────────────────────────────────────────────────


function _wire_preview_handlers(frm, d) {
    const $w = d.fields_dict.preview_html.$wrapper;

    $w.on('click', '.ps-show-diffs', function () {
        const item_code = $(this).data('item');
        _show_diff_drawer(d._plan, item_code);
    });

    $w.on('click', '.ps-adopt-by-sku', function () {
        d.hide();
        _open_adopt_by_sku(frm);
    });

    $w.on('click', '.ps-tree-filter', function () {
        const filter = $(this).data('filter');
        $w.find('.ps-tree-filter').removeClass('btn-primary').addClass('btn-default');
        $(this).removeClass('btn-default').addClass('btn-primary');
        const q = ($w.find('.ps-tree-search').val() || '').toLowerCase().trim();
        $w.find('.ps-tree-row').each(function () {
            const action = $(this).data('action');
            const matches_filter = filter === 'all' || filter === action;
            const matches_search = !q || ($(this).data('search') || '').includes(q);
            $(this).toggle(matches_filter && matches_search);
        });
    });

    $w.on('input', '.ps-tree-search', function () {
        const q = ($(this).val() || '').toLowerCase().trim();
        const active = $w.find('.ps-tree-filter.btn-primary').data('filter') || 'all';
        $w.find('.ps-tree-row').each(function () {
            const action = $(this).data('action');
            const matches_filter = active === 'all' || active === action;
            const matches_search = !q || ($(this).data('search') || '').includes(q);
            $(this).toggle(matches_filter && matches_search);
        });
    });
}


// ── Pre-Flight Dialog ───────────────────────────────────────────────


function _open_preflight_dialog(frm) {
    frappe.call({
        method: 'ecommerce_integrations.product_sync.api.preflight_check',
        args: { sync: frm.doc.name },
        freeze: true,
        freeze_message: __('Pre-flight running…'),
        callback(r) {
            if (r.message) _show_preflight_dialog(frm, r.message);
        },
    });
}


function _show_preflight_dialog(frm, result) {
    const sev_pill = {
        ok:    `<span class="indicator-pill green">${__('OK')}</span>`,
        warn:  `<span class="indicator-pill orange">${__('Warning')}</span>`,
        block: `<span class="indicator-pill red">${__('Blocker')}</span>`,
    };
    const rows = (result.findings || []).map((f) => `
        <tr>
            <td style="width:90px">${sev_pill[f.severity] || ''}</td>
            <td><code style="font-size:11px">${frappe.utils.escape_html(f.code)}</code></td>
            <td>${frappe.utils.escape_html(f.message)}</td>
        </tr>`).join('');
    const status_pill = result.ready_for_dryrun
        ? `<span class="indicator-pill green">${__('Ready for preview')}</span>`
        : `<span class="indicator-pill red">${__('Blockiert')}</span>`;
    const d = new frappe.ui.Dialog({
        title: __('Pre-Flight-Check'),
        size: 'large',
        fields: [{ fieldname: 'pf_html', fieldtype: 'HTML' }],
        primary_action_label: result.ready_for_dryrun ? __('Open preview') : __('Close'),
        primary_action() {
            d.hide();
            if (result.ready_for_dryrun) _open_preview_dialog(frm);
        },
    });
    d.fields_dict.pf_html.$wrapper.html(`
        <div style="margin-bottom:1em">${status_pill}</div>
        <table class="table table-sm" style="margin:0;font-size:12px">
            <thead><tr><th>${__('Severity')}</th><th>${__('Code')}</th><th>${__('Description')}</th></tr></thead>
            <tbody>${rows}</tbody>
        </table>`);
    d.show();
}


// ── Apply Live (with double-confirm) ────────────────────────────────


function _apply_live(frm) {
    frappe.warn(
        __('Apply Live auf Production-Backend?'),
        __('This push changes live data in {0}. Continue?',
            [frm.doc.backend || 'Backend']),
        () => frappe.confirm(
            __('Really apply? The action can only be undone via rollback.'),
            () => _do_apply_live(frm),
        ),
        __('Apply Live'),
        true,
    );
}


function _do_apply_live(frm) {
    const progressDialog = new frappe.ui.Dialog({
        title: __('Apply Live läuft im Hintergrund…'),
        size: 'small',
        fields: [{ fieldname: 'progress_html', fieldtype: 'HTML' }],
    });
    progressDialog.set_secondary_action_label(__('Schließen'));
    let closed = false;
    progressDialog.set_secondary_action(() => {
        closed = true;
        progressDialog.hide();
        frm.reload_doc();
    });

    const renderProgress = ({ percent = 0, current = 0, total = 0, status = 'queued', failed = 0, error = '' } = {}) => {
        const normalized = (status || 'queued').toLowerCase();
        let label;
        if (normalized === 'queued' || normalized === 'pending') {
            label = __('Queued — waiting for a long worker…');
        } else if (normalized === 'running' && !total) {
            label = __('Worker started — building live diff…');
        } else if (normalized === 'running') {
            label = `${current.toLocaleString()} / ${total.toLocaleString()} (${percent}%)`;
        } else if (normalized === 'ok') {
            label = __('Finished successfully.');
        } else if (normalized === 'partial') {
            label = __('Finished with {0} failed items.', [failed || 0]);
        } else if (normalized === 'error') {
            label = error || __('Apply failed.');
        } else {
            label = normalized;
        }
        const color = normalized === 'error' ? '#d93025'
            : normalized === 'partial' ? '#fa8c16'
            : 'var(--primary,#2490ef)';
        progressDialog.fields_dict.progress_html.$wrapper.html(`
            <div style="padding:8px 0;">
                <div class="progress" style="height:14px;margin-bottom:8px;">
                    <div class="progress-bar progress-bar-striped ${normalized === 'running' ? 'active' : ''}"
                         role="progressbar"
                         style="width:${percent || 4}%;background:${color};"></div>
                </div>
                <div class="text-muted small" style="text-align:center;">${frappe.utils.escape_html(label)}</div>
            </div>
        `);
    };

    const finish = (m) => {
        progressDialog.hide();
        const status = (m.status || 'unknown').toLowerCase();
        frappe.show_alert({
            message: __('Apply Status: {0}', [status]),
            indicator: status === 'ok' ? 'green' :
                      (status === 'partial' ? 'orange' : 'red'),
        });
        frm.reload_doc();
    };

    const poll = () => {
        if (closed) return;
        frappe.call({
            method: 'ecommerce_integrations.product_sync.api.get_apply_status',
            args: { sync: frm.doc.name },
            callback(rr) {
                if (closed) return;
                const m = rr.message || {};
                renderProgress(m);
                const status = (m.status || '').toLowerCase();
                if (['ok', 'partial', 'error'].includes(status)) {
                    finish(m);
                    return;
                }
                setTimeout(poll, 2000);
            },
            error() {
                if (closed) return;
                renderProgress({ status: 'error', error: __('Could not read apply status.') });
            },
        });
    };

    renderProgress();
    progressDialog.show();

    frappe.call({
        method: 'ecommerce_integrations.product_sync.api.start_apply',
        args: { sync: frm.doc.name, with_snapshot: true },
        callback(r) {
            const m = r.message || {};
            renderProgress(m);
            setTimeout(poll, 800);
        },
        error() {
            renderProgress({ status: 'error', error: __('Could not enqueue apply.') });
        },
    });
}


// ── Adopt by SKU ────────────────────────────────────────────────────


function _open_adopt_by_sku(frm) {
    frappe.confirm(
        __('Bestehende Backend-Produkte per SKU adoptieren? ' +
           'Findet existierende Backend-Produkte und verknüpft sie mit ERPNext-Items. ' +
           'Bereits zugeordnete Items werden übersprungen. Idempotent.'),
        () => frappe.call({
            method: 'ecommerce_integrations.product_sync.api.auto_adopt_by_sku',
            args: { sync: frm.doc.name, also_try_ean: true },
            freeze: true,
            freeze_message: __('SKU matching running…'),
            callback(r) {
                if (!r.message) return;
                const m = r.message;
                frappe.msgprint({
                    title: __('Adoption abgeschlossen'),
                    message: `
                        <table class="table table-sm" style="margin-bottom:0">
                          <tr><th class="text-right">${__('Adoptiert')}</th><td><strong>${m.adopted}</strong></td></tr>
                          <tr><th class="text-right">${__('Schon zugeordnet')}</th><td>${m.skipped_already_mapped}</td></tr>
                          <tr><th class="text-right">${__('Mehrdeutig')}</th><td>${m.ambiguous}</td></tr>
                          <tr><th class="text-right">${__('Kein Treffer')}</th><td>${m.no_match}</td></tr>
                        </table>`,
                    indicator: m.adopted > 0 ? 'green' : 'orange',
                });
            },
        }),
    );
}


// ── Test History Dialog ─────────────────────────────────────────────


function _open_history_dialog(frm) {
    frappe.call({
        method: 'ecommerce_integrations.product_sync.api.list_test_history',
        args: { sync: frm.doc.name, limit: 20 },
        callback(r) {
            const rows = (r.message || []).map((row) => `
                <tr><td><a href="/app/ecommerce-sync-run/${row.name}">${row.name}</a></td>
                    <td>${row.mode || '—'}</td>
                    <td><span class="indicator-pill ${_status_color(row.status)}">${row.status || '—'}</span></td>
                    <td>${row.items_total || 0}</td>
                    <td>${row.items_failed || 0}</td>
                    <td class="text-muted small">${row.started_at ? frappe.datetime.comment_when(row.started_at) : '—'}</td>
                </tr>`).join('');
            const d = new frappe.ui.Dialog({
                title: __('Test-Historie'),
                size: 'large',
                fields: [{ fieldname: 'h_html', fieldtype: 'HTML' }],
            });
            d.fields_dict.h_html.$wrapper.html(`
                <table class="table table-sm" style="font-size:12px">
                    <thead><tr>
                        <th>${__('Run')}</th><th>${__('Modus')}</th><th>${__('Status')}</th>
                        <th>${__('Items')}</th><th>${__('Fehler')}</th><th>${__('Gestartet')}</th>
                    </tr></thead>
                    <tbody>${rows || `<tr><td colspan="6" class="text-muted text-center">${__('No test runs yet.')}</td></tr>`}</tbody>
                </table>`);
            d.show();
        },
    });
}


function _status_color(s) {
    if (s === 'ok') return 'green';
    if (s === 'error') return 'red';
    if (s === 'partial') return 'orange';
    if (s === 'running') return 'blue';
    return 'gray';
}


// ── Styles (injected once) ──────────────────────────────────────────


function _inject_ps_styles_once() {
    if (document.getElementById('ps-dialog-styles')) return;
    const style = document.createElement('style');
    style.id = 'ps-dialog-styles';
    style.textContent = `
        .ps-filter-chips .btn { padding-top: 2px; padding-bottom: 2px; }
    `;
    document.head.appendChild(style);
}
