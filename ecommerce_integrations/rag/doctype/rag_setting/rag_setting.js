// Copyright (c) 2024, Frappe and contributors
// For license information, please see license.txt

frappe.ui.form.on('RAG Setting', {
    refresh: function(frm) {
        // Test Connection Button
        frm.add_custom_button(__('Test Connection'), function() {
            frappe.show_alert({message: __('Testing connection...'), indicator: 'blue'});
            frm.call('test_connection').then(r => {
                if (r.message && r.message.success) {
                    let msg = `
                        <strong>Provider:</strong> ${r.message.provider}<br>
                        <strong>Index:</strong> ${r.message.index_name}<br>
                        <strong>Index exists:</strong> ${r.message.index_exists ? 'Yes' : 'No'}
                    `;
                    if (r.message.vector_count !== undefined) {
                        msg += `<br><strong>Vectors:</strong> ${r.message.vector_count}`;
                        msg += `<br><strong>Dimensions:</strong> ${r.message.dimensions}`;
                    }
                    frappe.msgprint({
                        title: __('Connection Successful'),
                        indicator: 'green',
                        message: msg
                    });
                } else {
                    frappe.msgprint({
                        title: __('Connection Failed'),
                        indicator: 'red',
                        message: r.message ? r.message.error : 'Unknown error'
                    });
                }
            });
        });

        // Create Index Button (if index doesn't exist)
        frm.add_custom_button(__('Create Index'), function() {
            frappe.confirm(
                __('This will create a new Pinecone index. Continue?'),
                function() {
                    frappe.show_alert({message: __('Creating index...'), indicator: 'blue'});
                    frm.call('create_index').then(r => {
                        if (r.message) {
                            if (r.message.status === 'created') {
                                frappe.msgprint({
                                    title: __('Index Created'),
                                    indicator: 'green',
                                    message: `Index "${r.message.index_name}" created with ${r.message.dimensions} dimensions`
                                });
                            } else if (r.message.status === 'exists') {
                                frappe.msgprint({
                                    title: __('Index Already Exists'),
                                    indicator: 'blue',
                                    message: `Index "${r.message.index_name}" already exists`
                                });
                            }
                        }
                    });
                }
            );
        }, __('Actions'));

        // Full Sync Button
        frm.add_custom_button(__('Full Sync'), function() {
            frappe.confirm(
                __('This will sync all products to Pinecone. Continue?'),
                function() {
                    frm.call('start_full_sync').then(r => {
                        frappe.show_alert({
                            message: __('Full Sync started - check background jobs'),
                            indicator: 'green'
                        });
                    });
                }
            );
        }, __('Actions'));

        // Force Process Queue Button
        if (frm.doc.enable_bulk_sync) {
            frm.add_custom_button(__('Process Queue Now'), function() {
                frm.call('force_process_queue').then(r => {
                    frappe.show_alert({
                        message: __('Queue processing started'),
                        indicator: 'green'
                    });
                    // Refresh status after a moment
                    setTimeout(() => frm.reload_doc(), 2000);
                });
            }, __('Actions'));
        }

        // Load Queue Status
        if (frm.doc.enabled) {
            frm.call('get_queue_status').then(r => {
                if (r.message) {
                    let status = r.message;
                    let html = `
                        <div class="rag-queue-status" style="padding: 10px; background: var(--bg-light-gray); border-radius: 4px;">
                            <p style="margin: 5px 0;"><strong>Queue Size:</strong> ${status.queue_size || 0}</p>
                            <p style="margin: 5px 0;"><strong>Bulk Mode:</strong>
                                <span class="indicator-pill ${status.bulk_mode_active ? 'green' : 'gray'}">
                                    ${status.bulk_mode_active ? 'Active' : 'Inactive'}
                                </span>
                            </p>
                            <p style="margin: 5px 0;"><strong>Processing:</strong>
                                <span class="indicator-pill ${status.processing ? 'orange' : 'gray'}">
                                    ${status.processing ? 'Yes' : 'No'}
                                </span>
                            </p>
                        </div>
                    `;
                    frm.fields_dict.queue_status_html.$wrapper.html(html);
                }
            });
        }
    },

    embedding_provider: function(frm) {
        // Auto-update dimensions when provider changes
        frm.trigger('update_dimensions');
    },

    embedding_model: function(frm) {
        // Auto-update dimensions when model changes
        frm.trigger('update_dimensions');
    },

    update_dimensions: function(frm) {
        let dims = 1536; // default
        if (frm.doc.embedding_provider === 'Google') {
            dims = 768;
        } else if (frm.doc.embedding_provider === 'OpenAI') {
            if (frm.doc.embedding_model === 'text-embedding-3-large') {
                dims = 3072;
            } else {
                dims = 1536;
            }
        } else {
            dims = 768; // Local sentence-transformers
        }
        frm.set_value('dimensions', dims);
    }
});
