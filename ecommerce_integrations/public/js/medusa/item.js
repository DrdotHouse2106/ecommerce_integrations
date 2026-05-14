/**
 * Medusa Item Form Extensions
 *
 * - Renders a per-item sync indicator (last sync time + integration item code).
 * - Adds an "Open in Medusa Admin" button if the item is already synced.
 * - Mirrors the Shopware Item form pattern (public/js/shopware6/item.js).
 */

frappe.ui.form.on('Item', {
    refresh: function(frm) {
        if (frm.is_new()) return;
        render_medusa_item_indicators(frm);
        if (typeof render_combined_channel_resolution === 'function') {
            render_combined_channel_resolution(frm);
        } else if (typeof _ec_render_combined_channel_resolution === 'function') {
            _ec_render_combined_channel_resolution(frm);
        }
    }
});


function _get_medusa_setting(callback) {
    if (window._medusa_setting_cache) {
        callback(window._medusa_setting_cache);
        return;
    }
    frappe.db.get_value('Medusa Setting', 'Medusa Setting', ['enable_medusa', 'medusa_url'])
        .then(r => {
            window._medusa_setting_cache = r.message || {};
            callback(window._medusa_setting_cache);
        });
}


function render_medusa_item_indicators(frm) {
    _get_medusa_setting(function(setting) {
        if (!setting || !setting.enable_medusa) return;

        frappe.db.get_list('Ecommerce Item', {
            filters: {
                erpnext_item_code: frm.doc.item_code,
                integration: 'medusa'
            },
            fields: ['integration_item_code', 'modified', 'sku', 'has_variants'],
            limit: 20
        }).then(rows => {
            if (!rows || !rows.length) return;
            rows.forEach(row => {
                let when = row.modified ? frappe.datetime.comment_when(row.modified) : '';
                let label = when
                    ? __('Medusa: {0} (last sync {1})', [row.integration_item_code, when])
                    : __('Medusa: {0}', [row.integration_item_code]);
                frm.dashboard.add_indicator(label, 'green');
            });

            // Add "Open in Medusa Admin" button if we have a Medusa URL and an id
            let medusa_url = (setting.medusa_url || '').replace(/\/+$/, '');
            let first = rows[0];
            if (medusa_url && first && first.integration_item_code) {
                let admin_url = `${medusa_url}/app/products/${first.integration_item_code}`;
                frm.add_custom_button(
                    __('Open in Medusa Admin'),
                    function() { window.open(admin_url, '_blank'); },
                    __('Medusa')
                );
            }
        });
    });
}


/* -------------------------------------------------------------------------
 * Combined cross-backend channel resolution card — fallback definition.
 *
 * The canonical implementation lives in public/js/shopware6/item.js (so
 * the Shopware-only operator still gets the card). If that file is also
 * loaded, ``render_combined_channel_resolution`` is already defined and
 * this fallback is unused. If Shopware is disabled or its bundle isn't
 * loaded on this site, this fallback fires instead.
 * ----------------------------------------------------------------------- */

if (typeof render_combined_channel_resolution === 'undefined') {
    window._ec_render_combined_channel_resolution = function(frm) {
        if (window._ecommerce_channel_card_rendered === frm.doc.name) return;
        window._ecommerce_channel_card_rendered = frm.doc.name;

        frappe.call({
            method: 'ecommerce_integrations.catalog_mirror.api.resolve_item_for_form',
            args: { item_code: frm.doc.item_code },
            callback: function(r) {
                if (!r || !r.message) {
                    frm.dashboard.add_indicator(
                        __('Resolver not yet available — run bench migrate'),
                        'orange'
                    );
                    return;
                }
                _ec_render_card(frm, r.message);
            },
            error: function() {
                frm.dashboard.add_indicator(
                    __('Resolver not yet available — run bench migrate'),
                    'orange'
                );
            }
        });
    };

    function _ec_render_card(frm, data) {
        const shopware = (data && data.shopware) || { channels: [], categories: [], excluded_channels: [], warnings: [] };
        const medusa = (data && data.medusa) || { channels: [], categories: [], excluded_channels: [], warnings: [] };

        const html = $(`
            <div class="ecommerce-channel-resolution" style="margin: 10px 0; padding: 10px;
                 border: 1px solid var(--border-color); border-radius: 6px; background: var(--fg-color);">
                <h6 style="margin-top:0;">${frappe.utils.escape_html(__('Ecommerce Visibility (live, computed)'))}</h6>
                <div class="ec-visibility"></div>
                <h6 style="margin-top:14px;">${frappe.utils.escape_html(__('Categories (would be linked next sync)'))}</h6>
                <div class="ec-categories"></div>
                <div style="margin-top:10px;">
                    <button class="btn btn-xs btn-default ec-edit-overrides">
                        ${frappe.utils.escape_html(__('Edit Channel Overrides'))}
                    </button>
                </div>
            </div>
        `);

        html.find('.ec-visibility').append(_ec_render_visibility('Shopware', shopware));
        html.find('.ec-visibility').append(_ec_render_visibility('Medusa', medusa));
        html.find('.ec-categories').append(_ec_render_categories('Shopware', shopware));
        html.find('.ec-categories').append(_ec_render_categories('Medusa', medusa));

        html.find('.ec-edit-overrides').on('click', function() {
            frm.scroll_to_field('ecommerce_channel_overrides');
        });

        let $host = frm.dashboard && frm.dashboard.wrapper
            ? $(frm.dashboard.wrapper).find('.form-dashboard-section.ecommerce-channel-host')
            : $();
        if (!$host.length) {
            $host = $(`<div class="form-dashboard-section ecommerce-channel-host"></div>`);
            if (frm.dashboard && frm.dashboard.wrapper) {
                $(frm.dashboard.wrapper).append($host);
            }
        }
        $host.empty().append(html);
    }

    function _ec_render_visibility(backend_label, payload) {
        const rows = [];
        const included = payload.channels || [];
        const excluded = payload.excluded_channels || [];

        rows.push(`<div style="font-weight:600; margin-top:6px;">${frappe.utils.escape_html(backend_label)}:</div>`);

        if (!included.length && !excluded.length) {
            rows.push(`<div style="padding-left:14px; color: var(--text-muted);">
                ${frappe.utils.escape_html(__('(none — backend not yet configured)'))}
            </div>`);
            return rows.join('');
        }

        included.forEach(function(c) {
            const name = frappe.utils.escape_html(c.sales_channel || '');
            const vis = frappe.utils.escape_html(String(c.visibility || ''));
            rows.push(`<div style="padding-left:14px;">
                <span style="color: var(--green-600);">&#10003;</span>
                <span>${name}</span>
                <span style="color: var(--text-muted); margin-left:8px;">${__('visibility')} ${vis}</span>
                <span style="margin-left:8px;">${_ec_format_source(c)}</span>
            </div>`);
        });

        excluded.forEach(function(c) {
            const name = frappe.utils.escape_html(c.sales_channel || '');
            rows.push(`<div style="padding-left:14px; color: var(--text-muted);">
                <span>&#8856;</span>
                <span>${name}</span>
                <span style="margin-left:8px;">${frappe.utils.escape_html(__('(excluded)'))}</span>
                <span style="margin-left:8px;">${_ec_format_source(c)}</span>
            </div>`);
        });

        return rows.join('');
    }

    function _ec_render_categories(backend_label, payload) {
        const cats = payload.categories || [];
        const out = [];
        out.push(`<div style="font-weight:600; margin-top:6px;">${frappe.utils.escape_html(backend_label)}:</div>`);
        if (!cats.length) {
            out.push(`<div style="padding-left:14px; color: var(--text-muted);">
                ${frappe.utils.escape_html(__('(none — backend not yet configured)'))}
            </div>`);
            return out.join('');
        }
        cats.forEach(function(c) {
            const path = frappe.utils.escape_html(c.path || c.sales_channel || '');
            out.push(`<div style="padding-left:14px;">
                <span>&bull;</span>
                <span>${path}</span>
                <span style="margin-left:8px;">${_ec_format_source(c)}</span>
            </div>`);
        });
        return out.join('');
    }

    function _ec_format_source(entry) {
        const source = entry.source || 'default';
        const doc = entry.source_doc;
        function tag(label, href) {
            const esc = frappe.utils.escape_html(label);
            if (href) {
                return `<a href="${href}" target="_blank">[${esc}]</a>`;
            }
            return `<span style="color: var(--text-muted);">[${esc}]</span>`;
        }
        if (source === 'override:include' || source === 'override:exclude') {
            return tag(__('from Override'));
        }
        if (source === 'default') {
            return tag(__('from default'));
        }
        if (source.indexOf('mirror:') === 0) {
            const name = source.slice('mirror:'.length);
            const href = doc
                ? `/app/ecommerce-catalog-mirror/${encodeURIComponent(doc)}`
                : `/app/ecommerce-catalog-mirror/${encodeURIComponent(name)}`;
            return tag(__('from Mirror: {0}', [name]), href);
        }
        if (source.indexOf('smart_collection:') === 0) {
            const name = source.slice('smart_collection:'.length);
            const href = doc
                ? `/app/ecommerce-smart-collection/${encodeURIComponent(doc)}`
                : `/app/ecommerce-smart-collection/${encodeURIComponent(name)}`;
            return tag(__('from SC: {0}', [name]), href);
        }
        return tag(source);
    }
}
