/**
 * Shopware 6 Item Group Form Extensions
 *
 * Adds Full Resync to Shopware button on the Item Group form.
 */

frappe.ui.form.on('Item Group', {
    refresh: function(frm) {
        add_shopware_category_resync_button(frm);
    }
});


function add_shopware_category_resync_button(frm) {
    if (frm.is_new()) return;

    // Skip root categories
    const root_categories = ['All Item Groups'];
    if (root_categories.includes(frm.doc.name)) {
        return;
    }

    // Check if Shopware integration is enabled
    frappe.call({
        method: 'frappe.client.get_value',
        args: {
            doctype: 'Shopware Setting',
            fieldname: 'enable_shopware'
        },
        callback: function(r) {
            if (!r.message || !r.message.enable_shopware) {
                return;
            }

            // Add Full Resync button under Shopware group
            frm.add_custom_button(
                __('Komplett-Resync zu Shopware'),
                function() {
                    sync_category_to_shopware(frm);
                },
                __('Shopware')
            );

            // Style the button
            frm.change_custom_button_type(__('Komplett-Resync zu Shopware'), __('Shopware'), 'primary');
        }
    });
}


function sync_category_to_shopware(frm) {
    const item_group_name = frm.doc.name;

    // Show confirmation dialog
    frappe.confirm(
        __('Dies synchronisiert die Kategorie "{0}" mit Shopware.<br><br>Die Kategorie-Hierarchie (übergeordnete Kategorien) wird bei Bedarf ebenfalls synchronisiert.', [item_group_name]),
        function() {
            frappe.call({
                method: 'ecommerce_integrations.shopware6.product_export.sync_category_to_shopware',
                args: {
                    item_group_name: item_group_name
                },
                freeze: true,
                freeze_message: __('Kategorie wird mit Shopware synchronisiert...'),
                callback: function(r) {
                    if (r.message) {
                        if (r.message.success) {
                            frappe.msgprint({
                                title: __('Sync erfolgreich'),
                                indicator: 'green',
                                message: r.message.message || __('Kategorie erfolgreich mit Shopware synchronisiert')
                            });
                            frm.reload_doc();
                        } else {
                            frappe.msgprint({
                                title: __('Sync fehlgeschlagen'),
                                indicator: 'red',
                                message: r.message.message || __('Kategorie konnte nicht mit Shopware synchronisiert werden')
                            });
                        }
                    }
                },
                error: function(r) {
                    frappe.msgprint({
                        title: __('Fehler'),
                        indicator: 'red',
                        message: __('Kategorie konnte nicht mit Shopware synchronisiert werden. Details siehe Error Log.')
                    });
                }
            });
        }
    );
}
