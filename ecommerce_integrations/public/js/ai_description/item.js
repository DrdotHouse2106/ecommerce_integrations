/**
 * AI Description Generation - Item Form Extensions
 *
 * Adds AI description generation button and indicators to the Item form.
 */

frappe.ui.form.on('Item', {
    refresh: function(frm) {
        // Add AI generation button
        add_ai_description_button(frm);

        // Show AI generation status indicator
        show_ai_status_indicator(frm);

        // Add copy to description button if AI description exists
        add_copy_description_button(frm);

        // Add sync to Shopware button if AI description exists
        add_sync_to_shopware_button(frm);
    }
});


function add_ai_description_button(frm) {
    if (frm.is_new()) return;

    // Check if already generated
    const already_generated = frm.doc.ai_description_generated;

    // Add primary button
    frm.add_custom_button(
        already_generated ? __('Regenerate AI Description') : __('Generate AI Description'),
        function() {
            generate_ai_description(frm);
        },
        __('AI Description')
    );

    // If already generated, add option to mark as reviewed
    if (already_generated && !frm.doc.ai_description_reviewed) {
        frm.add_custom_button(
            __('Mark as Reviewed'),
            function() {
                mark_as_reviewed(frm);
            },
            __('AI Description')
        );
    }
}


function show_ai_status_indicator(frm) {
    if (frm.is_new()) return;

    // Clear existing indicators
    frm.dashboard.clear_headline();

    if (frm.doc.ai_description_generated) {
        const date = frm.doc.ai_generation_date
            ? frappe.datetime.str_to_user(frm.doc.ai_generation_date)
            : '';

        let status_text = __('AI Description generated');
        if (date) {
            status_text += ` (${date})`;
        }
        if (frm.doc.ai_model_used) {
            status_text += ` - ${frm.doc.ai_model_used}`;
        }

        if (frm.doc.ai_description_reviewed) {
            frm.dashboard.add_indicator(
                status_text + ' - ' + __('Reviewed'),
                'green'
            );
        } else {
            frm.dashboard.add_indicator(
                status_text + ' - ' + __('Pending Review'),
                'blue'
            );
        }
    } else {
        // Check if item has description content
        if (!frm.doc.description || frm.doc.description.trim() === '') {
            frm.dashboard.add_indicator(
                __('No description - AI generation recommended'),
                'orange'
            );
        }
    }
}


function add_copy_description_button(frm) {
    if (frm.is_new()) return;
    if (!frm.doc.ai_long_description) return;

    frm.add_custom_button(
        __('Copy to Main Description'),
        function() {
            frappe.confirm(
                __('This will overwrite the main description field. Continue?'),
                function() {
                    frappe.call({
                        method: 'ecommerce_integrations.ai_description.api.copy_ai_to_main_description',
                        args: { item_code: frm.doc.item_code },
                        freeze: true,
                        freeze_message: __('Copying description...'),
                        callback: function(r) {
                            if (r.message && r.message.success) {
                                frappe.show_alert({
                                    message: __('Description copied successfully'),
                                    indicator: 'green'
                                });
                                frm.reload_doc();
                            } else {
                                frappe.msgprint({
                                    title: __('Error'),
                                    indicator: 'red',
                                    message: r.message.error || __('Failed to copy description')
                                });
                            }
                        }
                    });
                }
            );
        },
        __('AI Description')
    );
}


function add_sync_to_shopware_button(frm) {
    if (frm.is_new()) return;
    if (!frm.doc.ai_long_description) return;

    frm.add_custom_button(
        __('Sync to Shopware'),
        function() {
            frappe.call({
                method: 'ecommerce_integrations.ai_description.api.sync_ai_description_to_shopware',
                args: { item_code: frm.doc.item_code },
                freeze: true,
                freeze_message: __('Syncing to Shopware...'),
                callback: function(r) {
                    if (r.message && r.message.success) {
                        frappe.show_alert({
                            message: r.message.message || __('Synced successfully'),
                            indicator: 'green'
                        });
                        frm.reload_doc();
                    } else {
                        frappe.msgprint({
                            title: __('Error'),
                            indicator: 'red',
                            message: r.message.error || __('Failed to sync')
                        });
                    }
                }
            });
        },
        __('AI Description')
    );
}


function generate_ai_description(frm) {
    frappe.call({
        method: 'ecommerce_integrations.ai_description.api.generate_description_for_item',
        args: {
            item_code: frm.doc.item_code
        },
        freeze: true,
        freeze_message: __('Generating AI description... This may take a moment.'),
        callback: function(r) {
            if (r.message) {
                if (r.message.success) {
                    frappe.show_alert({
                        message: __('AI description generated successfully!'),
                        indicator: 'green'
                    });

                    // Show generation time
                    if (r.message.generation_time) {
                        frappe.show_alert({
                            message: __('Generation time: {0}s', [r.message.generation_time.toFixed(2)]),
                            indicator: 'blue'
                        }, 3);
                    }

                    // Reload the form to show new content
                    frm.reload_doc();
                } else {
                    frappe.msgprint({
                        title: __('Generation Failed'),
                        indicator: 'red',
                        message: r.message.error || __('Unknown error occurred')
                    });
                }
            }
        },
        error: function(r) {
            frappe.msgprint({
                title: __('Error'),
                indicator: 'red',
                message: __('Failed to generate description. Check the error log for details.')
            });
        }
    });
}


function mark_as_reviewed(frm) {
    frappe.call({
        method: 'ecommerce_integrations.ai_description.api.mark_as_reviewed',
        args: {
            item_code: frm.doc.item_code,
            reviewed: true
        },
        callback: function(r) {
            if (r.message && r.message.success) {
                frappe.show_alert({
                    message: __('Marked as reviewed'),
                    indicator: 'green'
                });
                frm.reload_doc();
            }
        }
    });
}


// Preview benefits as formatted list
frappe.ui.form.on('Item', 'ai_benefits', function(frm) {
    if (frm.doc.ai_benefits) {
        try {
            const benefits = JSON.parse(frm.doc.ai_benefits);
            if (Array.isArray(benefits) && benefits.length > 0) {
                const html = '<ul>' + benefits.map(b => `<li>${b}</li>`).join('') + '</ul>';
                // Could add a preview field here
            }
        } catch (e) {
            // Invalid JSON, ignore
        }
    }
});
