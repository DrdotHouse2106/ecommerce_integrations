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
                __('Full Resync to Shopware'),
                function() {
                    sync_category_to_shopware(frm);
                },
                __('Shopware')
            );

            // Style the button
            frm.change_custom_button_type(__('Full Resync to Shopware'), __('Shopware'), 'primary');
        }
    });
}


function sync_category_to_shopware(frm) {
    const item_group_name = frm.doc.name;

    // Show confirmation dialog
    frappe.confirm(
        __('This will sync the category "{0}" to Shopware.<br><br>The category hierarchy (parent categories) will also be synced if needed.', [item_group_name]),
        function() {
            frappe.call({
                method: 'ecommerce_integrations.shopware6.product_export.sync_category_to_shopware',
                args: {
                    item_group_name: item_group_name
                },
                freeze: true,
                freeze_message: __('Syncing category to Shopware...'),
                callback: function(r) {
                    if (r.message) {
                        if (r.message.success) {
                            frappe.msgprint({
                                title: __('Sync Successful'),
                                indicator: 'green',
                                message: r.message.message || __('Category synced successfully to Shopware')
                            });
                            frm.reload_doc();
                        } else {
                            frappe.msgprint({
                                title: __('Sync Failed'),
                                indicator: 'red',
                                message: r.message.message || __('Failed to sync category to Shopware')
                            });
                        }
                    }
                },
                error: function(r) {
                    frappe.msgprint({
                        title: __('Error'),
                        indicator: 'red',
                        message: __('Failed to sync category to Shopware. Check the error log for details.')
                    });
                }
            });
        }
    );
}
