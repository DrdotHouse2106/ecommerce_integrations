// Catalog Mirror form — rich tree-diff preview dialog (Phase 3).
//
// Mirrors the Smart Collections preview dialog's renderer style
// (`ecommerce_smart_collection.js`): same `__()` calls, same
// `frappe.utils.escape_html`, same indicator-pill vocabulary, so an
// operator who learnt one already understands the other.

frappe.ui.form.on('Ecommerce Catalog Mirror', {
    refresh(frm) {
        if (frm.is_new()) return;
        _render_dashboard_indicators(frm);
        _render_intro(frm);
        _render_header_buttons(frm);
    },
});


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
    // orphan_policy lives on the doc — the dry-run plan doesn't echo
    // it, so we surface it via the frm reference threaded through.
    const orphan_policy = (frm && frm.doc && frm.doc.orphan_policy) || 'keep';
    const root_live = plan.external_root_id
        ? frappe.utils.escape_html(plan.external_root_id)
        : `<em class="text-muted">${__('not yet resolved')}</em>`;
    const status_pill = plan.unresolved_external_root
        ? `<span class="indicator-pill yellow">${__('Unresolved root')}</span>`
        : `<span class="indicator-pill green">${__('Root resolved')}</span>`;

    const header = `
        <div style="margin-bottom:0.75em;padding:0.5em 0.75em;border:1px solid var(--border-color);border-radius:4px">
            <div>
                <strong>${frappe.utils.escape_html(plan.root_item_group || '—')}</strong>
                <span class="text-muted">→</span> ${root_live} ${status_pill}
            </div>
            <div class="text-muted small">
                ${__('Backend')}: ${frappe.utils.escape_html(plan.backend)}
                · ${__('Channel')}: ${frappe.utils.escape_html(plan.target_sales_channel || '—')}
            </div>
        </div>`;

    const pills = `
        <div style="margin-bottom:0.75em">
            <span class="indicator-pill green">${__('Create')}: ${c.create}</span>
            <span class="indicator-pill orange">${__('Update')}: ${c.update}</span>
            <span class="indicator-pill yellow">${__('Move')}: ${c.move}</span>
            <span class="indicator-pill gray">${__('Noop')}: ${c.noop}</span>
            <span class="indicator-pill red">${__('Orphan')}: ${c.orphan}</span>
            <span class="indicator-pill red">${__('Mapping drift')}: ${c.drift}</span>
        </div>`;

    const notes = (plan.notes || [])
        .map((n) => `<div class="text-muted small">${frappe.utils.escape_html(n)}</div>`)
        .join('');

    return header + pills + notes
        + _render_tree_section(plan)
        + _render_orphans_section(plan, orphan_policy)
        + _render_drift_section(plan);
}


// Reconstruct the tree from path strings ("A / B / C") because the
// differ doesn't ship explicit parent links. Nodes whose computed
// parent path isn't itself in the plan become tree roots.
function _render_tree_section(plan) {
    const all_nodes = [
        ...(plan.creates || []), ...(plan.updates || []),
        ...(plan.moves || []), ...(plan.noops || []),
    ];
    if (!all_nodes.length) {
        return `<div class="text-muted">${__('No tree nodes in plan.')}</div>`;
    }

    // Stamp the action on each node so the row renderer doesn't need
    // a second lookup, and build a path→node map for parent resolution.
    const by_path = {};
    for (const action of ['creates', 'updates', 'moves', 'noops']) {
        const single = action.slice(0, -1); // creates → create
        for (const node of plan[action] || []) {
            node._action = single;
            by_path[node.item_group_path || node.item_group] = node;
        }
    }

    const parent_path = (p) => {
        const parts = (p || '').split(' / ').filter(Boolean);
        return parts.length <= 1 ? null : parts.slice(0, -1).join(' / ');
    };

    const roots = [];
    const children_of = {};
    for (const node of all_nodes) {
        const p = node.item_group_path || node.item_group;
        const pp = parent_path(p);
        if (pp && by_path[pp]) {
            (children_of[pp] = children_of[pp] || []).push(node);
        } else {
            roots.push(node);
        }
    }

    // Small trees fully expanded; large trees only top 3 levels — the
    // collapsed branches still render their HTML (just `display:none`)
    // so toggling is instant and survives the page-down → page-up
    // refresh cycle without re-laying-out the world. Even >200 nodes
    // is a few KB of HTML; jQuery sets it in one call and the browser
    // never blocks.
    const auto_expand = all_nodes.length <= 50;
    const max_open_depth = auto_expand ? Infinity : 3;

    const sort_kids = (list) => list.slice().sort(
        (a, b) => (a.proposed_name || a.item_group).localeCompare(b.proposed_name || b.item_group),
    );

    function render_node(node, depth) {
        const path = node.item_group_path || node.item_group;
        const kids = sort_kids(children_of[path] || []);
        const collapsed = depth >= max_open_depth;
        const arrow = kids.length
            ? `<span class="cm-toggle" style="cursor:pointer;display:inline-block;width:1em">${collapsed ? '▸' : '▾'}</span>`
            : '<span style="display:inline-block;width:1em"></span>';
        const kid_html = kids.length
            ? `<ul style="list-style:none;padding-left:1.25em;margin:0;${collapsed ? 'display:none' : ''}">${kids.map((k) => render_node(k, depth + 1)).join('')}</ul>`
            : '';
        return `<li style="margin:0.1em 0">${arrow}${_render_node_row(node)}${kid_html}</li>`;
    }

    const tree_html = sort_kids(roots).map((r) => render_node(r, 0)).join('');
    return `
        <div style="margin-bottom:0.75em">
            <h6 style="margin-bottom:0.25em">${__('Tree diff')} <span class="text-muted small">(${all_nodes.length} ${__('node(s)')})</span></h6>
            <ul style="list-style:none;padding-left:0;margin:0;font-family:var(--font-stack-monospace, monospace);font-size:12px">${tree_html}</ul>
        </div>`;
}


function _render_node_row(node) {
    const action = node._action || node.action || 'noop';
    const v = _action_visuals(action);
    const name = frappe.utils.escape_html(node.proposed_name || node.item_group);
    const ig = frappe.utils.escape_html(node.item_group);

    let detail = '';
    if (action === 'create') {
        const parent = node.proposed_parent_external_id
            ? frappe.utils.escape_html(node.proposed_parent_external_id)
            : __('root');
        detail = `<span class="text-muted small"> — ${__('would create under {0}', [parent])}</span>
            <button class="btn btn-xs btn-default cm-adopt-create"
                data-ig="${ig}"
                data-parent-ext="${frappe.utils.escape_html(node.proposed_parent_external_id || '')}">
                ${__('Adopt existing…')}
            </button>`;
    } else if (action === 'update') {
        const diffs = [];
        if (node.current_name && node.current_name !== node.proposed_name) {
            diffs.push(`${__('name')}: '${frappe.utils.escape_html(node.current_name)}' → '${frappe.utils.escape_html(node.proposed_name)}'`);
        }
        if ((node.current_description || '') !== (node.proposed_description || '')) {
            diffs.push(__('description'));
        }
        if (node.current_active !== node.proposed_active) {
            diffs.push(`${__('active')}: ${node.current_active} → ${node.proposed_active}`);
        }
        if (diffs.length) detail = `<span class="text-muted small"> — ${diffs.join(', ')}</span>`;
    } else if (action === 'move') {
        const old_p = node.current_parent_external_id || __('root');
        const new_p = node.proposed_parent_external_id || __('root');
        detail = `<span class="text-muted small"> — ${__('parent: {0} → {1}',
            [frappe.utils.escape_html(old_p), frappe.utils.escape_html(new_p)])}</span>`;
    }

    const notes = (node.notes || [])
        .map((n) => ` <span class="text-warning small">⚠ ${frappe.utils.escape_html(n)}</span>`)
        .join('');

    return `${v.icon} <strong>${name}</strong>
        <span class="indicator-pill ${v.color}" style="margin-left:0.4em">${v.label}</span>
        ${detail}${notes}`;
}


function _action_visuals(action) {
    const map = {
        create: { icon: '➕', color: 'green', label: __('CREATE') },
        update: { icon: '✏', color: 'orange', label: __('UPDATE') },
        move: { icon: '⤴', color: 'yellow', label: __('MOVE') },
        noop: { icon: '·', color: 'gray', label: __('NOOP') },
        error: { icon: '⚠', color: 'red', label: __('ERROR') },
    };
    return map[action] || { icon: '·', color: 'gray', label: (action || '').toUpperCase() };
}


function _render_orphans_section(plan, orphan_policy) {
    const orphans = plan.orphans || [];
    if (!orphans.length) return '';
    const rows = orphans.map((o) => {
        const ext = frappe.utils.escape_html(o.external_id);
        const name = frappe.utils.escape_html(o.name || '');
        return `<tr>
            <td>${frappe.utils.escape_html(o.path || o.name || '')}</td>
            <td class="small"><code>${ext}</code></td>
            <td class="text-right">${o.product_count || 0}</td>
            <td class="small text-muted">${frappe.utils.escape_html(o.last_modified || '')}</td>
            <td>
                <button class="btn btn-xs btn-default cm-orphan-adopt" data-ext="${ext}" data-name="${name}">${__('Adopt → IG…')}</button>
                <button class="btn btn-xs btn-danger cm-orphan-delete" data-ext="${ext}">${__('Delete')}</button>
                <button class="btn btn-xs btn-default cm-orphan-keep" data-ext="${ext}">${__('Keep')}</button>
            </td>
        </tr>`;
    }).join('');
    return `
        <div class="alert alert-warning" style="margin-top:0.75em">
            <h6>${__('Backend categories without ERPNext match (per orphan_policy={0})',
                [frappe.utils.escape_html(orphan_policy || 'keep')])}</h6>
            <table class="table table-sm" style="margin-bottom:0">
                <thead><tr>
                    <th>${__('Path')}</th><th>${__('External ID')}</th>
                    <th class="text-right">${__('Products')}</th>
                    <th>${__('Last modified')}</th><th>${__('Actions')}</th>
                </tr></thead>
                <tbody>${rows}</tbody>
            </table>
        </div>`;
}


function _render_drift_section(plan) {
    const drift = plan.mapping_drift || [];
    if (!drift.length) return '';
    const rows = drift.map((d) => {
        const ig = frappe.utils.escape_html(d.item_group);
        const ext = frappe.utils.escape_html(d.stale_external_id);
        return `<tr>
            <td>${ig}</td>
            <td class="small"><code>${ext}</code></td>
            <td>${frappe.utils.escape_html(d.proposed_name || '')}</td>
            <td><button class="btn btn-xs btn-default cm-drift-clear" data-ig="${ig}">${__('Clear mapping & re-create')}</button></td>
        </tr>`;
    }).join('');
    return `
        <div class="alert alert-danger" style="margin-top:0.75em">
            <h6>${__('Stale mappings (Item Groups pointing at deleted backend categories)')}</h6>
            <table class="table table-sm" style="margin-bottom:0">
                <thead><tr>
                    <th>${__('Item Group')}</th><th>${__('Stale External ID')}</th>
                    <th>${__('Proposed Name')}</th><th>${__('Actions')}</th>
                </tr></thead>
                <tbody>${rows}</tbody>
            </table>
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
