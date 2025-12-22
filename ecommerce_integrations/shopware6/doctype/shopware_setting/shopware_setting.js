// Copyright (c) 2024, Frappe and contributors
// For license information, please see license.txt

frappe.ui.form.on('Shopware Setting', {
	onload: function(frm) {
		// Set naming series options from onload data
		if (frm.doc.__onload) {
			if (frm.doc.__onload.sales_order_series_options) {
				frm.set_df_property('sales_order_series', 'options',
					frm.doc.__onload.sales_order_series_options.split('\n'));
			}
			if (frm.doc.__onload.delivery_note_series_options) {
				frm.set_df_property('delivery_note_series', 'options',
					frm.doc.__onload.delivery_note_series_options.split('\n'));
			}
			if (frm.doc.__onload.sales_invoice_series_options) {
				frm.set_df_property('sales_invoice_series', 'options',
					frm.doc.__onload.sales_invoice_series_options.split('\n'));
			}
		}
	},

	refresh: function(frm) {
		// Load Bulk Sync Queue Status
		if (frm.doc.enable_bulk_sync && frm.doc.enable_shopware) {
			frm.trigger('load_queue_status');

			// Add Process Queue Now button
			frm.add_custom_button(__('Process Queue Now'), function() {
				frappe.call({
					method: 'ecommerce_integrations.shopware6.bulk_sync.force_process_queue',
					callback: function(r) {
						frappe.show_alert({
							message: __('Queue processing started'),
							indicator: 'green'
						});
						// Refresh status after a moment
						setTimeout(() => frm.trigger('load_queue_status'), 2000);
					}
				});
			}, __('Bulk Sync'));

			// Add Clear Queue button
			frm.add_custom_button(__('Clear Queue'), function() {
				frappe.confirm(
					__('This will clear all pending sync items. Continue?'),
					function() {
						frappe.call({
							method: 'ecommerce_integrations.shopware6.bulk_sync.clear_all_queues',
							callback: function(r) {
								frappe.show_alert({
									message: __('Queues cleared'),
									indicator: 'green'
								});
								frm.trigger('load_queue_status');
							}
						});
					}
				);
			}, __('Bulk Sync'));
		}

		// Add Test Connection button
		if (!frm.is_new()) {
			frm.add_custom_button(__('Test Connection'), function() {
				frm.call({
					method: 'test_connection',
					doc: frm.doc,
					freeze: true,
					freeze_message: __('Testing connection...')
				});
			});

			// Add Sync Old Orders button
			frm.add_custom_button(__('Sync Old Orders'), function() {
				let dialog = new frappe.ui.Dialog({
					title: __('Sync Old Orders from Shopware'),
					fields: [
						{
							fieldname: 'from_date',
							fieldtype: 'Date',
							label: __('From Date'),
							reqd: 1,
							default: frappe.datetime.add_months(frappe.datetime.get_today(), -1)
						},
						{
							fieldname: 'to_date',
							fieldtype: 'Date',
							label: __('To Date'),
							reqd: 1,
							default: frappe.datetime.get_today()
						},
						{
							fieldname: 'limit',
							fieldtype: 'Int',
							label: __('Maximum Orders'),
							default: 100,
							reqd: 1
						}
					],
					primary_action_label: __('Start Sync'),
					primary_action: function(values) {
						dialog.hide();
						frappe.call({
							method: 'ecommerce_integrations.shopware6.order.sync_orders_from_shopware',
							args: {
								from_date: values.from_date,
								to_date: values.to_date,
								limit: values.limit
							},
							freeze: true,
							freeze_message: __('Syncing orders from Shopware...'),
							callback: function(r) {
								if (r.message) {
									frappe.msgprint({
										title: __('Order Sync Complete'),
										message: __('Synced: {0}, Skipped: {1}, Errors: {2}',
											[r.message.synced || 0, r.message.skipped || 0, r.message.errors || 0]),
										indicator: (r.message.errors || 0) > 0 ? 'orange' : 'green'
									});
								}
							}
						});
					}
				});
				dialog.show();
			}, __('Actions'));

			// Add Sync Old Customers button
			frm.add_custom_button(__('Sync Customers'), function() {
				let dialog = new frappe.ui.Dialog({
					title: __('Sync Customers from Shopware'),
					fields: [
						{
							fieldname: 'limit',
							fieldtype: 'Int',
							label: __('Maximum Customers'),
							default: 100,
							reqd: 1
						}
					],
					primary_action_label: __('Start Sync'),
					primary_action: function(values) {
						dialog.hide();
						frappe.call({
							method: 'ecommerce_integrations.shopware6.customer.sync_customers_from_shopware',
							args: {
								limit: values.limit
							},
							freeze: true,
							freeze_message: __('Syncing customers from Shopware...'),
							callback: function(r) {
								if (r.message) {
									frappe.msgprint({
										title: __('Customer Sync Complete'),
										message: __('Synced: {0}, Skipped: {1}, Errors: {2}',
											[r.message.synced || 0, r.message.skipped || 0, r.message.errors || 0]),
										indicator: (r.message.errors || 0) > 0 ? 'orange' : 'green'
									});
								}
							}
						});
					}
				});
				dialog.show();
			}, __('Actions'));

			// Add Bulk Sync button
			frm.add_custom_button(__('Bulk Sync Products'), function() {
				let dialog = new frappe.ui.Dialog({
					title: __('Bulk Sync Products to Shopware'),
					fields: [
						{
							fieldname: 'limit',
							fieldtype: 'Int',
							label: __('Maximum Number of Products'),
							default: 100,
							reqd: 1
						},
						{
							fieldname: 'batch_size',
							fieldtype: 'Int',
							label: __('Batch Size'),
							description: __('Number of products to process in each batch'),
							default: 20,
							reqd: 1
						}
					],
					primary_action_label: __('Start Sync'),
					primary_action: function(values) {
						dialog.hide();
						frappe.call({
							method: 'ecommerce_integrations.shopware6.product_export.bulk_sync_items_to_shopware',
							args: {
								limit: values.limit,
								batch_size: values.batch_size
							},
							freeze: true,
							freeze_message: __('Syncing products to Shopware...'),
							callback: function(r) {
								if (r.message) {
									if (r.message.success) {
										let indicator = r.message.errors > 0 ? 'orange' : 'green';
										let message = r.message.errors > 0
											? __('Synced {0} of {1} products successfully. {2} errors occurred.',
												[r.message.synced, r.message.total, r.message.errors])
											: __('Successfully synced all {0} products.', [r.message.synced]);

										frappe.msgprint({
											title: __('Sync Complete'),
											message: message,
											indicator: indicator
										});
									} else {
										frappe.msgprint({
											title: __('Sync Failed'),
											message: r.message.message,
											indicator: 'red'
										});
									}
								}
							}
						});
					}
				});
				dialog.show();
			}, __('Actions'));

			// Add Reconciliation Sync button
			frm.add_custom_button(__('Reconciliation Sync'), function() {
				let dialog = new frappe.ui.Dialog({
					title: __('Reconciliation: ERPNext → Shopware'),
					fields: [
						{
							fieldname: 'info_html',
							fieldtype: 'HTML',
							options: `<div class="alert alert-info">
								<p><strong>Reconciliation Sync</strong> compares all ERPNext items with Shopware and syncs any differences.</p>
								<p>Fields compared: Name, Description, Price, Weight, Active Status</p>
							</div>`
						},
						{
							fieldname: 'limit',
							fieldtype: 'Int',
							label: __('Maximum Items to Check'),
							default: 100,
							reqd: 1
						},
						{
							fieldname: 'dry_run',
							fieldtype: 'Check',
							label: __('Dry Run (only check, no sync)'),
							default: 1,
							description: __('Check this to only see what would be synced without making changes')
						},
						{
							fieldname: 'sync_images',
							fieldtype: 'Check',
							label: __('Also sync images'),
							default: 0,
							description: __('Include image sync (slower)')
						},
						{
							fieldname: 'include_unlinked',
							fieldtype: 'Check',
							label: __('Include unlinked items'),
							default: 0,
							description: __('Also sync items not yet in Shopware')
						},
						{
							fieldname: 'run_in_background',
							fieldtype: 'Check',
							label: __('Run in background'),
							default: 0,
							description: __('For large syncs (500+ items), run as background job')
						}
					],
					primary_action_label: __('Start'),
					primary_action: function(values) {
						dialog.hide();

						if (values.run_in_background) {
							// Enqueue background job
							frappe.call({
								method: 'ecommerce_integrations.shopware6.product_export.enqueue_full_reconciliation',
								args: {
									limit: values.limit,
									dry_run: values.dry_run,
									sync_images: values.sync_images,
									include_unlinked: values.include_unlinked
								},
								callback: function(r) {
									if (r.message && r.message.success) {
										frappe.show_alert({
											message: __('Reconciliation job started in background'),
											indicator: 'green'
										});
									}
								}
							});
						} else {
							// Run directly with freeze
							frappe.call({
								method: 'ecommerce_integrations.shopware6.product_export.reconcile_all_to_shopware',
								args: {
									limit: values.limit,
									dry_run: values.dry_run,
									sync_images: values.sync_images
								},
								freeze: true,
								freeze_message: values.dry_run
									? __('Checking items for differences...')
									: __('Reconciling items with Shopware...'),
								callback: function(r) {
									if (r.message) {
										let stats = r.message.statistics || {};
										let indicator = stats.sync_failed > 0 ? 'orange' : 'green';

										let message_parts = [
											__('Checked: {0}', [stats.total_checked || 0]),
											__('In sync: {0}', [stats.in_sync || 0]),
											__('Out of sync: {0}', [stats.out_of_sync || 0])
										];

										if (!values.dry_run) {
											message_parts.push(__('Synced: {0}', [stats.synced || 0]));
											if (stats.sync_failed > 0) {
												message_parts.push(__('Failed: {0}', [stats.sync_failed]));
											}
										}

										let details = message_parts.join('<br>');

										// Show out-of-sync items in dry run
										if (values.dry_run && r.message.out_of_sync_items && r.message.out_of_sync_items.length > 0) {
											details += '<br><br><strong>' + __('Out of sync items:') + '</strong><ul>';
											r.message.out_of_sync_items.slice(0, 10).forEach(item => {
												details += `<li>${item.item_code}: ${item.differences.join(', ')}</li>`;
											});
											if (r.message.out_of_sync_items.length > 10) {
												details += `<li>... and ${r.message.out_of_sync_items.length - 10} more</li>`;
											}
											details += '</ul>';
										}

										frappe.msgprint({
											title: values.dry_run ? __('Reconciliation Check Complete') : __('Reconciliation Sync Complete'),
											message: details,
											indicator: indicator
										});
									}
								}
							});
						}
					}
				});
				dialog.show();
			}, __('Actions'));

			// Add Clear Cache button
			frm.add_custom_button(__('Clear Cache'), function() {
				frappe.call({
					method: 'ecommerce_integrations.shopware6.product_export.clear_shopware_cache',
					callback: function(r) {
						if (r.message && r.message.status === 'success') {
							frappe.show_alert({
								message: __('Shopware caches cleared'),
								indicator: 'green'
							});
						}
					}
				});
			}, __('Actions'));
		}
	},

	load_queue_status: function(frm) {
		// Load and display bulk sync queue status
		frappe.call({
			method: 'ecommerce_integrations.shopware6.bulk_sync.get_queue_status',
			callback: function(r) {
				if (r.message) {
					let status = r.message;
					let html = `
						<div class="bulk-sync-status" style="padding: 15px; background: var(--bg-light-gray); border-radius: 4px; margin-top: 10px;">
							<h6 style="margin-bottom: 10px; font-weight: bold;">Bulk Sync Queue Status</h6>
							<p style="margin: 5px 0;"><strong>Queue Size:</strong> ${status.queue_size || 0}
								<span class="text-muted">(Products: ${status.product_queue_size || 0}, Properties: ${status.properties_queue_size || 0})</span>
							</p>
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

					// Show queued items if any
					if (status.product_queue && status.product_queue.length > 0) {
						html += `
							<div style="margin-top: 10px; font-size: 12px;">
								<strong>Queued Products (first 10):</strong>
								<ul style="margin: 5px 0; padding-left: 20px;">
									${status.product_queue.map(item => `<li>${item}</li>`).join('')}
								</ul>
							</div>
						`;
					}

					if (frm.fields_dict.bulk_sync_status_html) {
						frm.fields_dict.bulk_sync_status_html.$wrapper.html(html);
					}
				}
			}
		});
	}
});
