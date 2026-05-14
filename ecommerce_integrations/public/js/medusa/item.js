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
