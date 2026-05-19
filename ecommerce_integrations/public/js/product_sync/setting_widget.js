// Product Sync + Pull Sync widget rendered into Medusa Setting / Shopware Setting.
// Loaded via `doctype_js` in hooks.py for both forms; the parent setting JS
// calls window.ecom_product_sync_widget.render(frm, '<Backend>').
//
// Mirrors the catalog_mirror/setting_widget.js pattern — same layout
// language, two tables instead of one. The top table lists Push Syncs
// (ERP -> Shop, "Ecommerce Product Sync"), the bottom table lists Pull
// Syncs (Shop -> ERP, "Ecommerce Pull Sync"). Each renders into its own
// HTML field on the Setting form (ecom_product_sync_html and
// ecom_pull_sync_html).

(function () {

    // ─── Shared badges ───────────────────────────────────────────────

    function status_badge(row) {
        const map = {
            ok: 'green', error: 'red', running: 'blue',
            partial: 'orange', pending: 'orange', skipped: 'gray',
        };
        const colour = map[row.sync_status] || 'gray';
        const label = row.sync_status || 'pending';
        return `<span class="indicator-pill ${colour}">${frappe.utils.escape_html(label)}</span>`;
    }

    function active_badge(row) {
        if (row.is_active) return `<span class="text-success">${__('active')}</span>`;
        return `<span class="text-muted">${__('inactive')}</span>`;
    }

    function fmt_dt(value) {
        if (!value) return '<span class="text-muted">—</span>';
        return `<span class="text-muted small">${frappe.datetime.str_to_user(value)}</span>`;
    }

    function fmt_int(value) {
        if (value === undefined || value === null || value === '') return '0';
        return String(value);
    }

    // ─── Push Sync (ERP → Shop) ──────────────────────────────────────

    function build_push_table_html(rows) {
        let html = '<div class="ecom-product-sync-summary">';
        const cols = '<th>' + __('Title') + '</th>'
            + '<th style="width:110px">' + __('Status') + '</th>'
            + '<th style="width:90px">' + __('Active') + '</th>'
            + '<th style="width:160px">' + __('Last Synced') + '</th>'
            + '<th style="width:110px">' + __('In Scope') + '</th>'
            + '<th style="width:110px">' + __('Synced') + '</th>'
            + '<th style="width:60px">' + __('Open') + '</th>';
        html += `<table class="table table-sm" style="margin-bottom:0">`
            + `<thead><tr>${cols}</tr></thead><tbody>`;

        rows.forEach(function (row) {
            const link = `/app/ecommerce-product-sync/${encodeURIComponent(row.name)}`;
            html += '<tr>';
            html += `<td>${frappe.utils.escape_html(row.title || row.name)}</td>`;
            html += `<td>${status_badge(row)}</td>`;
            html += `<td>${active_badge(row)}</td>`;
            html += `<td>${fmt_dt(row.last_synced_at)}</td>`;
            html += `<td>${fmt_int(row.items_in_scope)}</td>`;
            html += `<td>${fmt_int(row.items_synced)}</td>`;
            html += `<td><a href="${link}">→</a></td>`;
            html += '</tr>';
        });
        html += '</tbody></table>';
        html += '</div>';
        return html;
    }

    function open_push_create_dialog(backend, on_created) {
        const d = new frappe.ui.Dialog({
            title: __('New Product Sync — {0}', [backend]),
            fields: [
                {
                    fieldname: 'title', fieldtype: 'Data', label: __('Bezeichnung'), reqd: 1,
                    description: __('Eigene Bezeichnung, z.B. "Hauptshop: Bestseller".'),
                },
                {
                    fieldname: 'scope_mode', fieldtype: 'Select', label: __('Scope mode'),
                    options: 'Catalog Mirror\nItem Group Subtree\nSmart Collection\nCustom Filter\nAll',
                    default: 'Item Group Subtree', reqd: 1,
                    description: __('Which items the sync covers. Refine via the form.'),
                },
                // Conditional fields appear depending on scope_mode selection.
                {
                    fieldname: 'linked_catalog_mirror', fieldtype: 'Link',
                    options: 'Ecommerce Catalog Mirror', label: __('Linked category sync'),
                    depends_on: "eval:doc.scope_mode==='Catalog Mirror'",
                    mandatory_depends_on: "eval:doc.scope_mode==='Catalog Mirror'",
                    get_query: function () {
                        return { filters: { backend: backend } };
                    },
                },
                {
                    fieldname: 'linked_smart_collection', fieldtype: 'Link',
                    options: 'Ecommerce Smart Collection', label: __('Linked collection'),
                    depends_on: "eval:doc.scope_mode==='Smart Collection'",
                    mandatory_depends_on: "eval:doc.scope_mode==='Smart Collection'",
                },
                {
                    fieldname: 'root_item_group', fieldtype: 'Link',
                    options: 'Item Group', label: __('Wurzel-Artikelgruppe'),
                    depends_on: "eval:doc.scope_mode==='Item Group Subtree'",
                    mandatory_depends_on: "eval:doc.scope_mode==='Item Group Subtree'",
                },
                { fieldname: 'col_break_1', fieldtype: 'Column Break' },
                {
                    fieldname: 'target_sales_channel', fieldtype: 'Autocomplete',
                    label: __('Ziel-Storefront (UUID)'),
                    description: __('UUID from Setting → Available Storefronts. One here, more via the form.'),
                },
                {
                    fieldname: 'is_active', fieldtype: 'Check', label: __('Aktiv (am Cron teilnehmen)'),
                    default: 0,
                    description: __('Leave off until pre-flight + dry-run are both green.'),
                },
            ],
            primary_action_label: __('Create & Edit'),
            primary_action: function (values) {
                const doc = {
                    doctype: 'Ecommerce Product Sync',
                    title: values.title,
                    backend: backend,
                    scope_mode: values.scope_mode || 'Item Group Subtree',
                    is_active: values.is_active ? 1 : 0,
                };
                if (values.scope_mode === 'Catalog Mirror') {
                    doc.linked_catalog_mirror = values.linked_catalog_mirror;
                } else if (values.scope_mode === 'Smart Collection') {
                    doc.linked_smart_collection = values.linked_smart_collection;
                } else if (values.scope_mode === 'Item Group Subtree') {
                    doc.root_item_group = values.root_item_group;
                    doc.include_descendants = 1;
                }
                if (values.target_sales_channel) {
                    doc.target_sales_channels = [{
                        sales_channel_id: values.target_sales_channel,
                        is_primary: 1,
                    }];
                }
                frappe.call({
                    method: 'frappe.client.insert',
                    args: { doc: doc },
                    callback: function (r) {
                        d.hide();
                        if (r.message) {
                            frappe.show_alert({
                                message: __('Sync angelegt. Vor "Aktiv"-Schaltung Pre-Flight + Dry-Run laufen lassen.'),
                                indicator: 'green',
                            });
                            if (on_created) on_created(r.message.name);
                        }
                    },
                });
            },
        });

        // Populate the storefront autocomplete from the parent's Sales-Channel-table.
        d.$wrapper.on('shown.bs.modal', function () {
            const field = d.fields_dict.target_sales_channel;
            if (!field || typeof field.set_data !== 'function') return;
            const method = backend === 'Shopware'
                ? 'ecommerce_integrations.catalog_mirror.api.list_shopware_sales_channels'
                : 'ecommerce_integrations.catalog_mirror.api.list_medusa_sales_channels';
            frappe.call({
                method: method,
                callback: function (r) {
                    field.set_data(r.message || []);
                },
            });
        });

        d.show();
    }

    function render_push(frm, backend) {
        const wrap = frm.fields_dict.ecom_product_sync_html;
        if (!wrap || !wrap.$wrapper) return;

        const list_url = `/app/ecommerce-product-sync/view/list?backend=${encodeURIComponent(backend)}`;
        const header = `
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
                <div>
                    <button class="btn btn-primary btn-sm ps-add-btn" type="button">
                        <i class="fa fa-plus"></i> ${__('Neuer Sync')}
                    </button>
                    <a class="btn btn-default btn-sm" href="${list_url}" target="_blank">${__('Browse all')}</a>
                </div>
                <button class="btn btn-default btn-xs ps-refresh-btn" type="button">${__('Refresh')}</button>
            </div>
        `;

        function reload() {
            wrap.$wrapper.html(header + '<div class="ps-body text-muted small">' + __('Loading…') + '</div>');
            wrap.$wrapper.find('.ps-add-btn').on('click', function () {
                open_push_create_dialog(backend, function (name) {
                    reload();
                    if (name) frappe.set_route('Form', 'Ecommerce Product Sync', name);
                });
            });
            wrap.$wrapper.find('.ps-refresh-btn').on('click', reload);

            frappe.call({
                method: 'ecommerce_integrations.product_sync.api.list_for_backend',
                args: { backend: backend },
                callback: function (r) {
                    const rows = r.message || [];
                    const $body = wrap.$wrapper.find('.ps-body');
                    if (!rows.length) {
                        $body.html(
                            `<div class="text-muted small">${__('No Product Syncs target {0} yet — use the Neuer Sync button above. One Sync per scope (storefront × Item Group / filter).', [backend])}</div>`
                        );
                        return;
                    }
                    $body.html(build_push_table_html(rows));
                },
                error: function () {
                    wrap.$wrapper.find('.ps-body').html(
                        `<div class="text-muted small">${__('Product Sync module not available — run bench migrate.')}</div>`
                    );
                },
            });
        }

        reload();
    }

    // ─── Pull Sync (Shop → ERP) ──────────────────────────────────────

    function build_pull_table_html(rows) {
        let html = '<div class="ecom-pull-sync-summary">';
        const cols = '<th>' + __('Title') + '</th>'
            + '<th style="width:110px">' + __('Status') + '</th>'
            + '<th style="width:90px">' + __('Active') + '</th>'
            + '<th style="width:120px">' + __('Cron') + '</th>'
            + '<th style="width:160px">' + __('Last Synced') + '</th>'
            + '<th style="width:110px">' + __('Pulled') + '</th>'
            + '<th style="width:110px">' + __('Failed') + '</th>'
            + '<th style="width:130px">' + __('Action') + '</th>'
            + '<th style="width:60px">' + __('Open') + '</th>';
        html += `<table class="table table-sm" style="margin-bottom:0">`
            + `<thead><tr>${cols}</tr></thead><tbody>`;

        rows.forEach(function (row) {
            const link = `/app/ecommerce-pull-sync/${encodeURIComponent(row.name)}`;
            const cron = row.cron_preset || '—';
            html += '<tr>';
            html += `<td>${frappe.utils.escape_html(row.title || row.name)}</td>`;
            html += `<td>${status_badge(row)}</td>`;
            html += `<td>${active_badge(row)}</td>`;
            html += `<td class="small text-muted">${frappe.utils.escape_html(cron)}</td>`;
            html += `<td>${fmt_dt(row.last_synced_at)}</td>`;
            html += `<td>${fmt_int(row.items_pulled_total)}</td>`;
            html += `<td>${fmt_int(row.items_failed_total)}</td>`;
            html += `<td>`
                + `<button class="btn btn-xs btn-default pull-run-btn" `
                + `data-sync="${frappe.utils.escape_html(row.name)}" type="button">`
                + `${__('Run Now')}</button>`
                + `</td>`;
            html += `<td><a href="${link}">→</a></td>`;
            html += '</tr>';
        });
        html += '</tbody></table>';
        html += '</div>';
        return html;
    }

    function open_pull_create_dialog(backend, on_created) {
        const d = new frappe.ui.Dialog({
            title: __('New Pull Sync — {0}', [backend]),
            fields: [
                {
                    fieldname: 'title', fieldtype: 'Data', label: __('Title'), reqd: 1,
                    description: __('Operator-facing label. Example: "yourshop.com / Hourly orders".'),
                },
                {
                    fieldname: 'pull_orders', fieldtype: 'Check', label: __('Pull Orders'), default: 1,
                },
                {
                    fieldname: 'pull_customers', fieldtype: 'Check', label: __('Pull Customers'), default: 0,
                },
                {
                    fieldname: 'pull_inventory', fieldtype: 'Check', label: __('Pull Inventory'), default: 0,
                },
                {
                    fieldname: 'cron_preset', fieldtype: 'Select', label: __('Cron preset'),
                    options: 'every_5_min\nevery_15_min\nhourly\ndaily',
                    default: 'every_15_min',
                },
                {
                    fieldname: 'is_active', fieldtype: 'Check', label: __('Active'), default: 0,
                    description: __('Leave off until the targets are configured, then activate from the form.'),
                },
            ],
            primary_action_label: __('Create & Edit'),
            primary_action: function (values) {
                frappe.call({
                    method: 'frappe.client.insert',
                    args: {
                        doc: {
                            doctype: 'Ecommerce Pull Sync',
                            title: values.title,
                            backend: backend,
                            pull_orders: values.pull_orders ? 1 : 0,
                            pull_customers: values.pull_customers ? 1 : 0,
                            pull_inventory: values.pull_inventory ? 1 : 0,
                            cron_preset: values.cron_preset || 'every_15_min',
                            is_active: values.is_active ? 1 : 0,
                        },
                    },
                    callback: function (r) {
                        d.hide();
                        if (r.message) {
                            frappe.show_alert({
                                message: __('Created. Configure targets, then activate.'),
                                indicator: 'green',
                            });
                            if (on_created) on_created(r.message.name);
                        }
                    },
                });
            },
        });
        d.show();
    }

    function run_pull_now(sync_name, on_done) {
        frappe.call({
            method: 'ecommerce_integrations.product_sync.pull_tasks.run_pull_now',
            args: { sync: sync_name },
            callback: function (r) {
                const result = r.message || {};
                const status = (result.status || 'unknown').toLowerCase();
                const indicator = (status === 'queued') ? 'blue'
                    : (status === 'running') ? 'orange'
                    : (status === 'error') ? 'red' : 'green';
                frappe.show_alert({
                    message: result.message || __('Pull queued: {0}', [status]),
                    indicator: indicator,
                });
                if (on_done) on_done();
            },
            error: function () {
                frappe.show_alert({
                    message: __('Pull could not be queued — see Error Log.'),
                    indicator: 'red',
                });
            },
        });
    }

    function render_pull(frm, backend) {
        const wrap = frm.fields_dict.ecom_pull_sync_html;
        if (!wrap || !wrap.$wrapper) return;

        const list_url = `/app/ecommerce-pull-sync/view/list?backend=${encodeURIComponent(backend)}`;
        const header = `
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
                <div>
                    <button class="btn btn-primary btn-sm pull-add-btn" type="button">
                        <i class="fa fa-plus"></i> ${__('Neuer Pull Sync')}
                    </button>
                    <a class="btn btn-default btn-sm" href="${list_url}" target="_blank">${__('Browse all')}</a>
                </div>
                <button class="btn btn-default btn-xs pull-refresh-btn" type="button">${__('Refresh')}</button>
            </div>
        `;

        function reload() {
            wrap.$wrapper.html(header + '<div class="pull-body text-muted small">' + __('Loading…') + '</div>');
            wrap.$wrapper.find('.pull-add-btn').on('click', function () {
                open_pull_create_dialog(backend, function (name) {
                    reload();
                    if (name) frappe.set_route('Form', 'Ecommerce Pull Sync', name);
                });
            });
            wrap.$wrapper.find('.pull-refresh-btn').on('click', reload);

            frappe.call({
                method: 'ecommerce_integrations.product_sync.pull_tasks.list_pull_syncs_for_backend',
                args: { backend: backend },
                callback: function (r) {
                    const rows = r.message || [];
                    const $body = wrap.$wrapper.find('.pull-body');
                    if (!rows.length) {
                        $body.html(
                            `<div class="text-muted small">${__('No Pull Syncs target {0} yet — use the Neuer Pull Sync button above. One Pull Sync per cadence (e.g. orders every 15 min).', [backend])}</div>`
                        );
                        return;
                    }
                    $body.html(build_pull_table_html(rows));
                    $body.find('.pull-run-btn').on('click', function () {
                        const sync_name = $(this).data('sync');
                        if (!sync_name) return;
                        run_pull_now(sync_name, reload);
                    });
                },
                error: function () {
                    wrap.$wrapper.find('.pull-body').html(
                        `<div class="text-muted small">${__('Pull Sync module not available — run bench migrate.')}</div>`
                    );
                },
            });
        }

        reload();
    }

    // ─── Public entry point ──────────────────────────────────────────

    function render(frm, backend) {
        render_push(frm, backend);
        render_pull(frm, backend);
    }

    window.ecom_product_sync_widget = { render: render };
})();
