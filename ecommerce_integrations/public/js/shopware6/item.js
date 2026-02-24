/**
 * Shopware 6 Item Form Extensions
 *
 * Adds Full Resync to Shopware button on the Item form.
 */

frappe.ui.form.on('Item', {
    refresh: function(frm) {
        add_shopware_resync_button(frm);
    }
});


function add_shopware_resync_button(frm) {
    if (frm.is_new()) return;

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

            // Style the button (optional: make it stand out)
            frm.change_custom_button_type(__('Full Resync to Shopware'), __('Shopware'), 'primary');
        }
    });
}


function sync_item_to_shopware(frm, is_template) {
    const item_code = frm.doc.item_code;

    // Show confirmation dialog with options
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

            // Determine which method to call
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

                            // Add variant count if applicable
                            if (r.message.variants_synced !== undefined) {
                                message += `<br><br><strong>${__('Variants synced')}:</strong> ${r.message.variants_synced}`;
                            }

                            // Add Shopware ID if available
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
