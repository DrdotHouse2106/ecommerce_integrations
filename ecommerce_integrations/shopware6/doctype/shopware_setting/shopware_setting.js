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

		// Add Refresh Sales Channels button handler
		frm.fields_dict.fetch_sales_channels_btn && frm.fields_dict.fetch_sales_channels_btn.$input &&
		frm.fields_dict.fetch_sales_channels_btn.$input.on('click', function() {
			frm.call({
				method: 'fetch_sales_channels',
				doc: frm.doc,
				freeze: true,
				freeze_message: __('Fetching Sales Channels from Shopware...'),
				callback: function(r) {
					if (r.message) {
						frm.reload_doc();
					}
				}
			});
		});

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

			// Consolidated ERPNext → Shopware Sync button
			frm.add_custom_button(__('ERPNext → Shopware'), function() {
				let dialog = new frappe.ui.Dialog({
					title: __('Sync ERPNext to Shopware'),
					size: 'large',
					fields: [
						{
							fieldname: 'sync_mode',
							fieldtype: 'Select',
							label: __('Sync Mode'),
							options: [
								{value: 'full', label: __('Full Reconciliation (Categories + Products)')},
								{value: 'categories', label: __('Categories Only')},
								{value: 'products', label: __('Products Only')},
								{value: 'cleanup', label: __('Cleanup Orphaned Categories')}
							],
							default: 'full',
							reqd: 1,
							onchange: function() {
								let mode = dialog.get_value('sync_mode');
								// Show/hide sections based on mode
								dialog.fields_dict.category_section.$wrapper.toggle(mode !== 'products');
								dialog.fields_dict.product_section.$wrapper.toggle(mode !== 'categories' && mode !== 'cleanup');
								dialog.fields_dict.cleanup_section.$wrapper.toggle(mode === 'full' || mode === 'cleanup');
								// Update info text
								let info_texts = {
									'full': `<div class="alert alert-info">
										<p><strong>Full Reconciliation</strong> performs a complete sync:</p>
										<ol>
											<li><strong>Phase 1:</strong> Sync ALL categories</li>
											<li><strong>Phase 2:</strong> Compare and update ALL products</li>
											<li><strong>Phase 3:</strong> Optional cleanup of orphaned categories</li>
										</ol>
									</div>`,
									'categories': `<div class="alert alert-info">
										<p><strong>Category Sync</strong> syncs ALL categories under the root to Shopware.</p>
										<p>Categories are processed in tree order (parent before children).</p>
									</div>`,
									'products': `<div class="alert alert-info">
										<p><strong>Product Sync</strong> compares ERPNext items with Shopware.</p>
										<p>Fields compared: Name, Description, Price, Weight, Active Status</p>
									</div>`,
									'cleanup': `<div class="alert alert-warning">
										<p><strong>⚠️ Cleanup</strong> deletes Shopware categories that no longer exist in ERPNext.</p>
										<p>Use with caution! Always do a dry run first.</p>
									</div>`
								};
								dialog.fields_dict.info_html.$wrapper.html(info_texts[mode] || '');
							}
						},
						{
							fieldname: 'info_html',
							fieldtype: 'HTML',
							options: `<div class="alert alert-info">
								<p><strong>Full Reconciliation</strong> performs a complete sync:</p>
								<ol>
									<li><strong>Phase 1:</strong> Sync ALL categories</li>
									<li><strong>Phase 2:</strong> Compare and update ALL products</li>
									<li><strong>Phase 3:</strong> Optional cleanup of orphaned categories</li>
								</ol>
							</div>`
						},
						// Category Options Section
						{
							fieldname: 'category_section',
							fieldtype: 'Section Break',
							label: __('Category Options')
						},
						{
							fieldname: 'category_root',
							fieldtype: 'Link',
							label: __('Root Category'),
							options: 'Item Group',
							default: frm.doc.category_sync_root || 'Produkte',
							reqd: 1
						},
						{
							fieldname: 'skip_root_category',
							fieldtype: 'Check',
							label: __('Skip Root Category'),
							default: frm.doc.skip_root_category || 1,
							description: __('Do not sync the root category itself')
						},
						{
							fieldname: 'col_break_cat',
							fieldtype: 'Column Break'
						},
						{
							fieldname: 'sync_empty_categories',
							fieldtype: 'Check',
							label: __('Include Empty Categories'),
							default: frm.doc.sync_empty_categories !== 0 ? 1 : 0,
							description: __('Sync categories even without products')
						},
						// Product Options Section
						{
							fieldname: 'product_section',
							fieldtype: 'Section Break',
							label: __('Product Options')
						},
						{
							fieldname: 'limit',
							fieldtype: 'Int',
							label: __('Product Limit'),
							default: 500,
							reqd: 1,
							description: __('Maximum products to process')
						},
						{
							fieldname: 'include_unlinked',
							fieldtype: 'Check',
							label: __('Include Unlinked Products'),
							default: 0,
							description: __('Also sync products not yet in Shopware')
						},
						{
							fieldname: 'col_break_prod',
							fieldtype: 'Column Break'
						},
						{
							fieldname: 'sync_images',
							fieldtype: 'Check',
							label: __('Sync Images'),
							default: 0,
							description: __('Also sync product images (slower)')
						},
						// Cleanup Options Section
						{
							fieldname: 'cleanup_section',
							fieldtype: 'Section Break',
							label: __('Cleanup Options'),
							collapsible: 1
						},
						{
							fieldname: 'cleanup_orphaned_categories',
							fieldtype: 'Check',
							label: __('Delete Orphaned Categories'),
							default: 0,
							description: __('⚠️ Delete Shopware categories that no longer exist in ERPNext')
						},
						// Execution Options Section
						{
							fieldname: 'execution_section',
							fieldtype: 'Section Break',
							label: __('Execution Options')
						},
						{
							fieldname: 'dry_run',
							fieldtype: 'Check',
							label: __('Dry Run'),
							default: 1,
							description: __('Preview changes without applying them')
						},
						{
							fieldname: 'col_break_exec',
							fieldtype: 'Column Break'
						},
						{
							fieldname: 'run_in_background',
							fieldtype: 'Check',
							label: __('Run in Background'),
							default: 1,
							description: __('Recommended for large syncs')
						}
					],
					primary_action_label: __('Start Sync'),
					primary_action: function(values) {
						dialog.hide();
						let mode = values.sync_mode;

						// Handle different sync modes
						if (mode === 'cleanup') {
							// Cleanup only
							frappe.call({
								method: 'ecommerce_integrations.shopware6.product_export.cleanup_orphaned_shopware_categories',
								args: {
									root_category: values.category_root,
									dry_run: values.dry_run
								},
								freeze: true,
								freeze_message: values.dry_run
									? __('Analyzing orphaned categories...')
									: __('Deleting orphaned categories...'),
								callback: function(r) {
									if (r.message) {
										let stats = r.message.statistics || {};
										let details = `
											<p><strong>Shopware Categories:</strong> ${stats.shopware_count || 0}</p>
											<p><strong>ERPNext Categories:</strong> ${stats.erpnext_count || 0}</p>
											<p><strong>Orphaned (to delete):</strong> ${stats.orphaned_count || 0}</p>
											<p><strong>Deleted:</strong> ${stats.deleted || 0}</p>
										`;
										if (r.message.orphaned_categories && r.message.orphaned_categories.length > 0) {
											details += '<br><strong>Orphaned categories:</strong><ul>';
											r.message.orphaned_categories.slice(0, 15).forEach(cat => {
												details += `<li>${cat}</li>`;
											});
											if (r.message.orphaned_categories.length > 15) {
												details += `<li>... and ${r.message.orphaned_categories.length - 15} more</li>`;
											}
											details += '</ul>';
										}
										frappe.msgprint({
											title: values.dry_run ? __('Cleanup Analysis') : __('Cleanup Complete'),
											message: details,
											indicator: stats.deleted > 0 ? 'orange' : 'green'
										});
									}
								}
							});
						} else if (mode === 'categories') {
							// Categories only
							frappe.call({
								method: 'ecommerce_integrations.shopware6.product_export.sync_all_categories_to_shopware',
								args: {
									root_category: values.category_root,
									skip_root: values.skip_root_category,
									sync_empty_categories: values.sync_empty_categories,
									dry_run: values.dry_run
								},
								freeze: true,
								freeze_message: values.dry_run
									? __('Analyzing categories...')
									: __('Syncing categories...'),
								callback: function(r) {
									if (r.message) {
										let stats = r.message.statistics || {};
										let details = `
											<p><strong>Total:</strong> ${stats.total || 0}</p>
											<p><strong>Synced:</strong> ${stats.synced || 0}</p>
											<p><strong>Skipped:</strong> ${stats.skipped || 0}</p>
											<p><strong>Errors:</strong> ${(stats.errors || []).length}</p>
										`;
										if (values.dry_run && r.message.synced_categories) {
											details += '<br><strong>Categories to sync:</strong><ul>';
											r.message.synced_categories.slice(0, 15).forEach(cat => {
												details += `<li>${cat}</li>`;
											});
											if (r.message.synced_categories.length > 15) {
												details += `<li>... and ${r.message.synced_categories.length - 15} more</li>`;
											}
											details += '</ul>';
										}
										frappe.msgprint({
											title: values.dry_run ? __('Dry Run Complete') : __('Category Sync Complete'),
											message: details,
											indicator: (stats.errors || []).length > 0 ? 'orange' : 'green'
										});
									}
								}
							});
						} else if (mode === 'products') {
							// Products only
							if (values.run_in_background) {
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
												message: __('Product sync started in background'),
												indicator: 'green'
											});
										}
									}
								});
							} else {
								frappe.call({
									method: 'ecommerce_integrations.shopware6.product_export.reconcile_all_to_shopware',
									args: {
										limit: values.limit,
										dry_run: values.dry_run,
										sync_images: values.sync_images
									},
									freeze: true,
									freeze_message: values.dry_run
										? __('Checking products...')
										: __('Syncing products...'),
									callback: function(r) {
										if (r.message) {
											let stats = r.message.statistics || {};
											let details = `
												<p><strong>Checked:</strong> ${stats.total_checked || 0}</p>
												<p><strong>In sync:</strong> ${stats.in_sync || 0}</p>
												<p><strong>Out of sync:</strong> ${stats.out_of_sync || 0}</p>
												<p><strong>Synced:</strong> ${stats.synced || 0}</p>
											`;
											if (values.dry_run && r.message.out_of_sync_items) {
												details += '<br><strong>Out of sync:</strong><ul>';
												r.message.out_of_sync_items.slice(0, 10).forEach(item => {
													details += `<li>${item.item_code}: ${item.differences.join(', ')}</li>`;
												});
												if (r.message.out_of_sync_items.length > 10) {
													details += `<li>... and ${r.message.out_of_sync_items.length - 10} more</li>`;
												}
												details += '</ul>';
											}
											frappe.msgprint({
												title: values.dry_run ? __('Product Check Complete') : __('Product Sync Complete'),
												message: details,
												indicator: stats.sync_failed > 0 ? 'orange' : 'green'
											});
										}
									}
								});
							}
						} else {
							// Full reconciliation
							if (values.run_in_background) {
								frappe.call({
									method: 'ecommerce_integrations.shopware6.product_export.enqueue_full_reconciliation_with_categories',
									args: {
										limit: values.limit,
										dry_run: values.dry_run,
										sync_images: values.sync_images,
										include_unlinked: values.include_unlinked,
										category_root: values.category_root,
										skip_root_category: values.skip_root_category,
										sync_empty_categories: values.sync_empty_categories,
										cleanup_orphaned_categories: values.cleanup_orphaned_categories
									},
									callback: function(r) {
										if (r.message && r.message.success) {
											frappe.show_alert({
												message: __('Full reconciliation started in background'),
												indicator: 'green'
											});
										}
									}
								});
							} else {
								frappe.call({
									method: 'ecommerce_integrations.shopware6.product_export.full_reconciliation',
									args: {
										limit: values.limit,
										dry_run: values.dry_run,
										sync_images: values.sync_images,
										include_unlinked: values.include_unlinked,
										category_root: values.category_root,
										skip_root_category: values.skip_root_category,
										sync_empty_categories: values.sync_empty_categories,
										cleanup_orphaned_categories: values.cleanup_orphaned_categories
									},
									freeze: true,
									freeze_message: __('Running full reconciliation...'),
									callback: function(r) {
										if (r.message) {
											let cat_stats = (r.message.category_sync || {}).statistics || {};
											let prod_stats = (r.message.product_sync || {}).statistics || {};
											let cleanup_stats = (r.message.cleanup || {}).statistics || {};

											let details = `
												<h5>📁 Category Sync</h5>
												<p>Total: ${cat_stats.total || 0}, Synced: ${cat_stats.synced || 0}, Errors: ${(cat_stats.errors || []).length}</p>
												<h5>📦 Product Sync</h5>
												<p>Checked: ${prod_stats.total_checked || 0}, In Sync: ${prod_stats.in_sync || 0}, Synced: ${prod_stats.synced || 0}</p>
											`;

											if (values.cleanup_orphaned_categories) {
												details += `
													<h5>🗑️ Cleanup</h5>
													<p>Orphaned: ${cleanup_stats.orphaned_count || 0}, Deleted: ${cleanup_stats.deleted || 0}</p>
												`;
											}

											if (values.dry_run) {
												details += '<p><em>(DRY RUN - no changes were made)</em></p>';
											}

											frappe.msgprint({
												title: __('Full Reconciliation Complete'),
												message: details,
												indicator: 'green'
											});
										}
									}
								});
							}
						}
					}
				});

				// Initialize visibility
				dialog.show();
				dialog.fields_dict.cleanup_section.$wrapper.show();
			}, __('Sync'));

			// Force Sync Options button (Price & Images)
			frm.add_custom_button(__('Force Sync'), function() {
				let dialog = new frappe.ui.Dialog({
					title: __('Force Sync to Shopware'),
					size: 'large',
					fields: [
						{
							fieldname: 'info_html',
							fieldtype: 'HTML',
							options: `<div class="alert alert-warning">
								<p><strong>⚠️ Force Sync</strong> deletes ALL existing data and recreates it from ERPNext.</p>
								<p>Use this to clean up duplicates or fix inconsistencies.</p>
							</div>`
						},
						{
							fieldname: 'sync_type',
							fieldtype: 'Select',
							label: __('Force Sync Type'),
							options: [
								{value: 'prices', label: __('Force Price Sync - Delete all prices & recreate')},
								{value: 'images', label: __('Force Image Sync - Delete all images & re-upload')}
							],
							default: 'prices',
							reqd: 1,
							onchange: function() {
								let type = dialog.get_value('sync_type');
								dialog.fields_dict.price_section.$wrapper.toggle(type === 'prices');
								dialog.fields_dict.image_section.$wrapper.toggle(type === 'images');
								dialog.fields_dict.dry_run.$wrapper.toggle(type === 'prices'); // dry_run only for prices

								let infos = {
									'prices': `<div class="alert alert-warning">
										<p><strong>⚠️ Force Price Sync</strong></p>
										<ul>
											<li>Deletes ALL advanced prices (rules, channel prices)</li>
											<li>Sets single clean price from ERPNext</li>
											<li>Ensures each product has only ONE price</li>
										</ul>
									</div>`,
									'images': `<div class="alert alert-warning">
										<p><strong>⚠️ Force Image Sync</strong></p>
										<ul>
											<li>Deletes ALL product images in Shopware</li>
											<li>Re-uploads all images from ERPNext</li>
											<li>Fixes broken image links or missing covers</li>
										</ul>
									</div>`
								};
								dialog.fields_dict.info_html.$wrapper.html(infos[type] || '');
							}
						},
						// Price Options Section
						{
							fieldname: 'price_section',
							fieldtype: 'Section Break',
							label: __('Price Sync Options')
						},
						{
							fieldname: 'price_list',
							fieldtype: 'Link',
							label: __('Price List'),
							options: 'Price List',
							default: 'Standard-Vertrieb',
							description: __('ERPNext Price List to use for prices')
						},
						{
							fieldname: 'col_break_price',
							fieldtype: 'Column Break'
						},
						{
							fieldname: 'price_batch_size',
							fieldtype: 'Int',
							label: __('Batch Size'),
							default: 20,
							description: __('Products per batch (20-50 recommended)')
						},
						// Image Options Section
						{
							fieldname: 'image_section',
							fieldtype: 'Section Break',
							label: __('Image Sync Options'),
							hidden: 1
						},
						{
							fieldname: 'image_batch_size',
							fieldtype: 'Int',
							label: __('Batch Size'),
							default: 20,
							description: __('Products per batch for image sync')
						},
						// Execution Options
						{
							fieldname: 'exec_section',
							fieldtype: 'Section Break',
							label: __('Execution')
						},
						{
							fieldname: 'dry_run',
							fieldtype: 'Check',
							label: __('Dry Run'),
							default: 0,
							description: __('Preview changes without applying')
						}
					],
					primary_action_label: __('Start Force Sync'),
					primary_action: function(values) {
						dialog.hide();

						if (values.sync_type === 'prices') {
							frappe.call({
								method: 'ecommerce_integrations.shopware6.export.price_handler.enqueue_force_sync_all_prices',
								args: {
									batch_size: values.price_batch_size || 20,
									price_list: values.price_list || 'Standard-Vertrieb',
									dry_run: values.dry_run
								},
								callback: function(r) {
									if (r.message && r.message.success) {
										frappe.show_alert({
											message: r.message.message,
											indicator: 'green'
										});
									}
								}
							});
						} else if (values.sync_type === 'images') {
							frappe.call({
								method: 'ecommerce_integrations.shopware6.export.reconciliation.enqueue_force_sync_all_images',
								args: {
									batch_size: values.image_batch_size || 20
								},
								callback: function(r) {
									if (r.message && r.message.success) {
										frappe.show_alert({
											message: r.message.message,
											indicator: 'green'
										});
									}
								}
							});
						}
					}
				});
				dialog.show();
				dialog.fields_dict.image_section.$wrapper.hide();
			}, __('Sync'));

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
