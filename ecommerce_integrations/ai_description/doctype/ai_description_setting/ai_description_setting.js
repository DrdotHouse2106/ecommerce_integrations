/**
 * AI Description Setting - Form Controller
 *
 * Handles test connection, batch processing triggers, and status display.
 */

frappe.ui.form.on('AI Description Setting', {
    refresh: function(frm) {
        // Add Test Connection button
        frm.add_custom_button(__('Test Connection'), function() {
            test_gemini_connection(frm);
        });

        // Add Reset Prompts button
        frm.add_custom_button(__('Reset Prompts to Default'), function() {
            frappe.confirm(
                __('This will reset both prompts to their default values. Continue?'),
                function() {
                    frm.call('reset_prompts').then(r => {
                        if (r.message && r.message.success) {
                            frappe.show_alert({
                                message: __('Prompts reset to defaults'),
                                indicator: 'green'
                            });
                            frm.reload_doc();
                        }
                    });
                }
            );
        });

        // Add Get Pending Items Count button
        frm.add_custom_button(__('Check Pending Items'), function() {
            frm.call('get_pending_items_count').then(r => {
                if (r.message) {
                    frappe.msgprint({
                        title: __('Pending Items'),
                        indicator: 'blue',
                        message: __('Found {0} items matching your filter criteria.', [r.message.count])
                    });
                }
            });
        });

        // Add Run Batch Now button if batch processing is enabled
        if (frm.doc.enable_batch_processing) {
            frm.add_custom_button(__('Run Batch Now'), function() {
                run_batch_processing(frm);
            }, __('Actions'));
        }

        // Update queue status display
        update_queue_status(frm);
    },

    enabled: function(frm) {
        // Show warning if enabling without API key
        if (frm.doc.enabled && !frm.doc.gemini_api_key) {
            frappe.msgprint({
                title: __('API Key Required'),
                indicator: 'orange',
                message: __('Please configure your Google Gemini API key before enabling.')
            });
        }
    },

    gemini_model: function(frm) {
        // Show model information
        const model_info = {
            'gemini-2.5-flash': 'Stable, balanced performance (recommended for production)',
            'gemini-2.5-flash-lite': 'Fastest, most cost-effective (good for high volume)',
            'gemini-3-flash-preview': 'Latest model, best quality (preview - Dec 2025)'
        };

        if (model_info[frm.doc.gemini_model]) {
            frappe.show_alert({
                message: model_info[frm.doc.gemini_model],
                indicator: 'blue'
            }, 5);
        }
    }
});


function test_gemini_connection(frm) {
    frappe.call({
        doc: frm.doc,
        method: 'test_connection',
        freeze: true,
        freeze_message: __('Testing Gemini API connection...'),
        callback: function(r) {
            if (r.message) {
                if (r.message.success) {
                    frappe.msgprint({
                        title: __('Connection Successful'),
                        indicator: 'green',
                        message: r.message.message + '<br><br><strong>Response:</strong> ' + r.message.response
                    });
                } else {
                    frappe.msgprint({
                        title: __('Connection Failed'),
                        indicator: 'red',
                        message: r.message.message
                    });
                }
            }
        }
    });
}


function run_batch_processing(frm) {
    frappe.confirm(
        __('This will generate AI descriptions for up to {0} items. Continue?', [frm.doc.batch_size]),
        function() {
            frappe.call({
                method: 'ecommerce_integrations.ai_description.api.generate_descriptions_batch',
                freeze: true,
                freeze_message: __('Generating descriptions... This may take several minutes.'),
                callback: function(r) {
                    if (r.message) {
                        frappe.msgprint({
                            title: __('Batch Processing Complete'),
                            indicator: r.message.failed > 0 ? 'orange' : 'green',
                            message: __(
                                'Processed {0} items: {1} successful, {2} failed.',
                                [r.message.total, r.message.success, r.message.failed]
                            )
                        });
                        frm.reload_doc();
                    }
                }
            });
        }
    );
}


function update_queue_status(frm) {
    // Fetch and display current status
    frappe.call({
        method: 'ecommerce_integrations.ai_description.api.get_ai_settings_summary',
        callback: function(r) {
            if (r.message && r.message.stats) {
                const stats = r.message.stats;
                const html = `
                    <div class="ai-queue-status">
                        <div class="row">
                            <div class="col-sm-3">
                                <div class="stat-box">
                                    <div class="stat-value">${stats.total_with_ai || 0}</div>
                                    <div class="stat-label">${__('Items with AI Description')}</div>
                                </div>
                            </div>
                            <div class="col-sm-3">
                                <div class="stat-box">
                                    <div class="stat-value">${stats.total_reviewed || 0}</div>
                                    <div class="stat-label">${__('Reviewed')}</div>
                                </div>
                            </div>
                            <div class="col-sm-3">
                                <div class="stat-box">
                                    <div class="stat-value text-warning">${stats.total_pending || 0}</div>
                                    <div class="stat-label">${__('Pending Generation')}</div>
                                </div>
                            </div>
                            <div class="col-sm-3">
                                <div class="stat-box">
                                    <div class="stat-value">${stats.items_processed || 0}</div>
                                    <div class="stat-label">${__('Total Processed')}</div>
                                </div>
                            </div>
                        </div>
                    </div>
                    <style>
                        .ai-queue-status .stat-box {
                            text-align: center;
                            padding: 15px;
                            background: var(--bg-color);
                            border-radius: 8px;
                            margin-bottom: 10px;
                        }
                        .ai-queue-status .stat-value {
                            font-size: 24px;
                            font-weight: bold;
                            color: var(--text-color);
                        }
                        .ai-queue-status .stat-label {
                            font-size: 12px;
                            color: var(--text-muted);
                            margin-top: 5px;
                        }
                        .ai-queue-status .text-warning {
                            color: var(--orange-500);
                        }
                    </style>
                `;
                frm.set_df_property('queue_status_html', 'options', html);
            }
        }
    });
}
