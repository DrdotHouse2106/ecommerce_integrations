/**
 * Shopware 6 Item Form Extensions
 *
 * - Adds Full Resync to Shopware button on the Item form.
 * - Renders a per-item sync indicator (last sync time + integration item code).
 * - Adds an "Open in Shopware Admin" button if the item is already synced.
 */

frappe.ui.form.on('Item', {
    refresh: function(frm) {
        if (frm.is_new()) return;
        render_shopware_item_indicators(frm);
        add_shopware_resync_button(frm);
        render_combined_channel_resolution(frm);
    }
});


function _get_shopware_setting(callback) {
    if (window._shopware_setting_cache) {
        callback(window._shopware_setting_cache);
        return;
    }
    frappe.db.get_value('Shopware Setting', 'Shopware Setting', ['enable_shopware', 'shop_url'])
        .then(r => {
            window._shopware_setting_cache = r.message || {};
            callback(window._shopware_setting_cache);
        });
}


function render_shopware_item_indicators(frm) {
    _get_shopware_setting(function(setting) {
        if (!setting || !setting.enable_shopware) return;

        frappe.db.get_list('Ecommerce Item', {
            filters: {
                erpnext_item_code: frm.doc.item_code,
                integration: 'shopware6'
            },
            fields: ['integration_item_code', 'modified', 'sku', 'has_variants'],
            limit: 20
        }).then(rows => {
            if (!rows || !rows.length) return;
            rows.forEach(row => {
                let when = row.modified ? frappe.datetime.comment_when(row.modified) : '';
                let label = when
                    ? __('Shopware: {0} (last sync {1})', [row.integration_item_code, when])
                    : __('Shopware: {0}', [row.integration_item_code]);
                frm.dashboard.add_indicator(label, 'green');
            });

            // Add "Open in Shopware Admin" button if we have a shop URL and an id
            let shop_url = (setting.shop_url || '').replace(/\/+$/, '');
            let first = rows[0];
            if (shop_url && first && first.integration_item_code) {
                let admin_url = `${shop_url}/admin#/sw/product/detail/${first.integration_item_code}`;
                frm.add_custom_button(
                    __('Open in Shopware Admin'),
                    function() { window.open(admin_url, '_blank'); },
                    __('Shopware')
                );
            }
        });
    });
}


function add_shopware_resync_button(frm) {
    _get_shopware_setting(function(setting) {
        if (!setting || !setting.enable_shopware) return;

        // Check if item is a template (has variants)
        const is_template = frm.doc.has_variants;

        // Add Full Resync button under Shopware group
        frm.add_custom_button(
            __('Full Resync to Shopware'),
            function() {
                sync_item_to_shopware(frm, is_template);
            },
            __('Shopware')
        );

        // Style the button
        frm.change_custom_button_type(__('Full Resync to Shopware'), __('Shopware'), 'primary');
    });
}


function sync_item_to_shopware(frm, is_template) {
    const item_code = frm.doc.item_code;

    let dialog = new frappe.ui.Dialog({
        title: __('Full Resync to Shopware'),
        fields: [
            {
                fieldtype: 'HTML',
                options: is_template
                    ? `<p>${__('This will sync the template product and all its variants to Shopware.')}</p>
                       <p><strong>${__('Item')}:</strong> ${item_code}</p>
                       <p class="text-muted">${__('All product data including images, prices, and properties will be synchronized.')}</p>`
                    : `<p>${__('This will sync the product with all its data to Shopware.')}</p>
                       <p><strong>${__('Item')}:</strong> ${item_code}</p>
                       <p class="text-muted">${__('Includes images, prices, properties, and all configured fields.')}</p>`
            },
            {
                fieldtype: 'Check',
                fieldname: 'include_variants',
                label: __('Include all variants'),
                default: 1,
                hidden: !is_template,
                description: is_template ? __('Sync all variant products along with the template') : ''
            }
        ],
        primary_action_label: __('Sync Now'),
        primary_action: function() {
            dialog.hide();

            const include_variants = is_template && dialog.get_value('include_variants');

            const method = include_variants
                ? 'ecommerce_integrations.shopware6.product_export.sync_template_with_variants_to_shopware'
                : 'ecommerce_integrations.shopware6.product_export.sync_item_to_shopware';

            const args = include_variants
                ? { template_item_code: item_code }
                : { item_code: item_code };

            frappe.call({
                method: method,
                args: args,
                freeze: true,
                freeze_message: include_variants
                    ? __('Syncing template and variants to Shopware...')
                    : __('Syncing to Shopware...'),
                callback: function(r) {
                    if (r.message) {
                        if (r.message.success) {
                            let message = r.message.message || __('Synced successfully to Shopware');

                            if (r.message.variants_synced !== undefined) {
                                message += `<br><br><strong>${__('Variants synced')}:</strong> ${r.message.variants_synced}`;
                            }

                            if (r.message.shopware_id) {
                                message += `<br><strong>${__('Shopware ID')}:</strong> ${r.message.shopware_id}`;
                            }

                            frappe.msgprint({
                                title: __('Sync Successful'),
                                indicator: 'green',
                                message: message
                            });

                            frm.reload_doc();
                        } else {
                            frappe.msgprint({
                                title: __('Sync Failed'),
                                indicator: 'red',
                                message: r.message.message || __('Failed to sync to Shopware. Check the error log for details.')
                            });
                        }
                    }
                },
                error: function(r) {
                    frappe.msgprint({
                        title: __('Error'),
                        indicator: 'red',
                        message: __('Failed to sync to Shopware. Check the error log for details.')
                    });
                }
            });
        }
    });

    dialog.show();
}


/* -------------------------------------------------------------------------
 * Combined cross-backend channel resolution card
 *
 * Calls the locked resolver endpoint (Phase 2) and renders a single
 * read-only card describing where this Item will land in each backend
 * (Shopware + Medusa). Sources are clickable.
 *
 * The same renderer ships in public/js/medusa/item.js — both copies are
 * idempotent thanks to the window._ecommerce_channel_card_rendered guard,
 * so the card is only painted once regardless of which item.js runs first.
 * ----------------------------------------------------------------------- */

function render_combined_channel_resolution(frm) {
    if (window._ecommerce_channel_card_rendered === frm.doc.name) return;
    window._ecommerce_channel_card_rendered = frm.doc.name;

    frappe.call({
        method: 'ecommerce_integrations.catalog_mirror.api.resolve_item_for_form',
        args: { item_code: frm.doc.item_code },
        callback: function(r) {
            if (!r || !r.message) {
                _render_resolver_unavailable(frm);
                return;
            }
            _render_channel_resolution_card(frm, r.message);
        },
        error: function() {
            _render_resolver_unavailable(frm);
        }
    });
}


function _render_resolver_unavailable(frm) {
    frm.dashboard.add_indicator(
        __('Resolver not yet available — run bench migrate'),
        'orange'
    );
}


function _render_channel_resolution_card(frm, data) {
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

    const $vis = html.find('.ec-visibility');
    $vis.append(_render_backend_visibility('Shopware', shopware));
    $vis.append(_render_backend_visibility('Medusa', medusa));

    const $cat = html.find('.ec-categories');
    $cat.append(_render_backend_categories('Shopware', shopware));
    $cat.append(_render_backend_categories('Medusa', medusa));

    html.find('.ec-edit-overrides').on('click', function() {
        frm.scroll_to_field('ecommerce_channel_overrides');
    });

    // Append below the dashboard. Use the form wrapper so it sits with
    // the other per-form widgets, not in a free-floating place.
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


function _render_backend_visibility(backend_label, payload) {
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
        const src_html = _format_source(c);
        rows.push(`<div style="padding-left:14px;">
            <span style="color: var(--green-600);">&#10003;</span>
            <span>${name}</span>
            <span style="color: var(--text-muted); margin-left:8px;">${__('visibility')} ${vis}</span>
            <span style="margin-left:8px;">${src_html}</span>
        </div>`);
    });

    excluded.forEach(function(c) {
        const name = frappe.utils.escape_html(c.sales_channel || '');
        const src_html = _format_source(c);
        rows.push(`<div style="padding-left:14px; color: var(--text-muted);">
            <span>&#8856;</span>
            <span>${name}</span>
            <span style="margin-left:8px;">${frappe.utils.escape_html(__('(excluded)'))}</span>
            <span style="margin-left:8px;">${src_html}</span>
        </div>`);
    });

    return rows.join('');
}


function _render_backend_categories(backend_label, payload) {
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
        const src_html = _format_source(c);
        out.push(`<div style="padding-left:14px;">
            <span>&bull;</span>
            <span>${path}</span>
            <span style="margin-left:8px;">${src_html}</span>
        </div>`);
    });
    return out.join('');
}


function _format_source(entry) {
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
