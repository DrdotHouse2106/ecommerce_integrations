// Copyright (c) 2024, Frappe and contributors
// For license information, please see license.txt

// Smart Collections widget (render + Add dialog) is loaded via doctype_js
// from public/js/smart_collections/setting_widget.js — exposes
// window.smart_collections_widget.render(frm, backend).

frappe.ui.form.on('Shopware Setting', {
	onload: function(frm) {
		if (frm.doc.__onload) {
			['sales_order_series', 'delivery_note_series', 'sales_invoice_series'].forEach(field => {
				let key = field + '_options';
				if (frm.doc.__onload[key]) {
					frm.set_df_property(field, 'options', frm.doc.__onload[key].split('\n'));
				}
			});
		}
	},

	refresh: function(frm) {
		if (window.catalog_mirror_widget) {
			window.catalog_mirror_widget.render(frm, 'Shopware');
		}
		if (window.smart_collections_widget) {
			window.smart_collections_widget.render(frm, 'Shopware');
		}
		// Track A UX surfaces — health banner, safety mode, webhook widget,
		// setup wizard. All four are no-ops on new (unsaved) forms.
		if (window.ecom_health_banner) {
			window.ecom_health_banner.render(frm, 'Shopware');
		}
		if (window.ecom_safety_mode) {
			window.ecom_safety_mode.attach_button(frm, 'Shopware');
		}
		if (window.ecom_webhook_widget) {
			window.ecom_webhook_widget.render(frm, 'Shopware');
		}
		if (window.shopware6_setup_wizard && !frm.is_new()) {
			const wizard_label = __('🧙 Setup Wizard');
			if (!frm.custom_buttons || !frm.custom_buttons[wizard_label]) {
				frm.add_custom_button(wizard_label, function() {
					window.shopware6_setup_wizard.open(frm);
				}).addClass('btn-primary');
			}
		}
		if (frm.fields_dict.fetch_sales_channels_btn && frm.fields_dict.fetch_sales_channels_btn.$input) {
			frm.fields_dict.fetch_sales_channels_btn.$input.off('click').on('click', function() {
				frm.call({
					method: 'fetch_sales_channels',
					doc: frm.doc,
					freeze: true,
					freeze_message: __('Fetching Sales Channels...'),
					callback: function() { frm.reload_doc(); }
				});
			});
		}

		if (frm.is_new()) return;

		// First-time setup intro (U14)
		if (!frm.doc.shop_url) {
			frm.set_intro(
				__('Setup: 1) Connection (Shop URL + Client ID/Secret). 2) Test Connection. 3) Refresh Sales Channels. 4) Company + Customer defaults. 5) Enable Upload only after a Dry-Run Complete Sync.'),
				'blue'
			);
		}

		// Health dashboard indicator (U11) — error count in last 24h for shopware6
		frappe.db.count('Ecommerce Integration Log', {
			filters: {
				integration: 'shopware6',
				status: 'Error',
				creation: ['>', frappe.datetime.add_days(frappe.datetime.now_datetime(), -1)]
			}
		}).then(count => {
			frm.dashboard.add_indicator(
				__('{0} sync errors in last 24h', [count]),
				count ? 'red' : 'green'
			);
		});

		// Complete Sync — primary action with safer dry-run default (U1)
		frm.add_custom_button(__('Complete Sync'), function() {
			let dlg = new frappe.ui.Dialog({
				title: __('Complete Sync'),
				size: 'large',
				fields: [
					{
						fieldname: 'info_html', fieldtype: 'HTML',
						options: '<div class="alert alert-info">' +
							'<strong>' + __('Complete Sync') + '</strong> ' +
							__('ensures Shopware matches ERPNext. Uses batch API for optimal performance.') +
							'</div>'
					},
					{ fieldtype: 'Section Break', label: __('Options') },
					{
						fieldname: 'sync_images', fieldtype: 'Check',
						label: __('Sync Images'), default: 1
					},
					{
						fieldname: 'sync_stock', fieldtype: 'Check',
						label: __('Sync Stock'), default: 0,
						description: __('Overwrites Shopware stock with ERPNext values')
					},
					{ fieldtype: 'Column Break' },
					{
						fieldname: 'category_root', fieldtype: 'Link',
						label: __('Root Category'), options: 'Item Group',
						default: frm.doc.category_sync_root || 'Products'
					},
					{
						fieldname: 'cleanup_orphans', fieldtype: 'Check',
						label: __('Clean Up Orphaned Entries'), default: 1
					},
					{ fieldtype: 'Section Break', label: __('Skip Phases'), collapsible: 1, collapsed: 1 },
					{ fieldname: 'skip_categories', fieldtype: 'Check', label: __('Skip Categories') },
					{ fieldname: 'skip_templates', fieldtype: 'Check', label: __('Skip Templates') },
					{ fieldname: 'skip_products', fieldtype: 'Check', label: __('Skip Products') },
					{ fieldtype: 'Column Break' },
					{ fieldname: 'skip_variants', fieldtype: 'Check', label: __('Skip Variants') },
					{ fieldname: 'skip_prices', fieldtype: 'Check', label: __('Skip Prices') },
					{ fieldtype: 'Section Break', label: __('Execution') },
					{
						fieldname: 'dry_run', fieldtype: 'Check',
						label: __('Dry Run'), default: 1,
						description: __('Preview changes without applying them. Recommended for first run.')
					}
				],
				primary_action_label: __('Preview Changes'),
				primary_action: function(values) {
					let run_sync = function() {
						dlg.hide();
						frappe.call({
							method: 'ecommerce_integrations.shopware6.sync.sync_manager.enqueue_full_reconciliation_no_brainer',
							args: {
								sync_images: values.sync_images ? 1 : 0,
								sync_stock: values.sync_stock ? 1 : 0,
								dry_run: values.dry_run ? 1 : 0,
								category_root: values.category_root,
								cleanup_orphan_categories: values.cleanup_orphans ? 1 : 0,
								skip_categories: values.skip_categories ? 1 : 0,
								skip_templates: values.skip_templates ? 1 : 0,
								skip_products: values.skip_products ? 1 : 0,
								skip_variants: values.skip_variants ? 1 : 0,
								skip_prices: values.skip_prices ? 1 : 0,
								skip_cleanup: !values.cleanup_orphans ? 1 : 0
							},
							callback: function(r) {
								if (r.message && r.message.success) {
									frappe.show_alert({
										message: values.dry_run
											? __('Dry run started. Check logs for results.')
											: __('Complete Sync started in background.'),
										indicator: 'green'
									});
								} else {
									frappe.msgprint({
										title: __('Error'),
										message: r.message?.message || __('Unknown error'),
										indicator: 'red'
									});
								}
							}
						});
					};
					if (!values.dry_run) {
						frappe.confirm(
							__('Run LIVE Complete Sync against {0}? This will create/update/delete categories and products. Run a Dry Run first if unsure.',
								[frm.doc.shop_url || 'Shopware']),
							run_sync
						);
					} else {
						run_sync();
					}
				}
			});
			// Switch primary button style + label based on dry_run (U1)
			let update_primary = function() {
				let is_dry = dlg.get_value('dry_run');
				dlg.set_primary_action(
					is_dry ? __('Preview Changes') : __('Start Live Sync'),
					dlg.primary_action
				);
				// btn-danger when live
				let $btn = dlg.get_primary_btn();
				if ($btn) {
					$btn.removeClass('btn-primary btn-danger');
					$btn.addClass(is_dry ? 'btn-primary' : 'btn-danger');
				}
			};
			dlg.fields_dict.dry_run.df.onchange = update_primary;
			dlg.show();
			update_primary();
		}).addClass('btn-primary-dark');

		// === Sync to Shopware group (U3) ===
		frm.add_custom_button(__('Categories'), function() {
			new frappe.ui.Dialog({
				title: __('Sync Categories'),
				fields: [
					{
						fieldname: 'category_root', fieldtype: 'Link',
						label: __('Root Category'), options: 'Item Group',
						default: frm.doc.category_sync_root || 'Products', reqd: 1
					},
					{ fieldname: 'skip_root', fieldtype: 'Check', label: __('Skip Root'), default: 1 },
					{ fieldname: 'dry_run', fieldtype: 'Check', label: __('Dry Run'), default: 1 }
				],
				primary_action_label: __('Sync'),
				primary_action: function(values) {
					this.hide();
					frappe.call({
						method: 'ecommerce_integrations.shopware6.product_export.sync_all_categories_to_shopware',
						args: values,
						freeze: true,
						freeze_message: __('Syncing categories...'),
						callback: function(r) {
							if (r.message) {
								let s = r.message.statistics || {};
								frappe.msgprint({
									title: __('Category Sync'),
									message: __('Total: {0}, Synced: {1}, Errors: {2}', [s.total || 0, s.synced || 0, (s.errors || []).length]),
									indicator: (s.errors || []).length ? 'orange' : 'green'
								});
							}
						}
					});
				}
			}).show();
		}, __('Sync to Shopware'));

		// Rebuild Prices (U2) — was "Force Price Sync"
		frm.add_custom_button(__('Rebuild Prices…'), function() {
			new frappe.ui.Dialog({
				title: __('Rebuild Prices'),
				fields: [
					{
						fieldname: 'warning_html', fieldtype: 'HTML',
						options: '<div class="alert alert-warning"><strong>' + __('Warning') + ':</strong> ' +
							__('This deletes all existing price rules in Shopware and recreates them from ERPNext. Run a Dry Run first.') +
							'</div>'
					},
					{ fieldname: 'batch_size', fieldtype: 'Int', label: __('Batch Size'), default: 20 },
					{ fieldtype: 'Section Break', label: __('Filter (optional)') },
					{ fieldname: 'item_group', fieldtype: 'Link', label: __('Item Group filter'), options: 'Item Group' },
					{ fieldtype: 'Section Break', label: __('Execution') },
					{
						fieldname: 'dry_run', fieldtype: 'Check',
						label: __('Dry Run'), default: 1,
						description: __('Preview the operation without applying.')
					}
				],
				primary_action_label: __('Start'),
				primary_action: function(values) {
					let run = () => {
						this.hide();
						frappe.call({
							method: 'ecommerce_integrations.shopware6.export.price_handler.enqueue_force_sync_all_prices',
							args: {
								batch_size: values.batch_size || 20,
								item_group: values.item_group || null,
								dry_run: values.dry_run ? 1 : 0
							},
							callback: function(r) {
								if (r.message && r.message.success) {
									frappe.show_alert({
										message: values.dry_run
											? __('Dry run started. Check logs for results.')
											: __('Price sync started in background.'),
										indicator: 'green'
									});
								}
							}
						});
					};
					if (!values.dry_run) {
						frappe.confirm(__('Delete and recreate all price rules in Shopware?'), run);
					} else {
						run();
					}
				}
			}).show();
		}, __('Sync to Shopware'));

		// Rebuild Variants (U2) — was "Force Variant Sync"
		frm.add_custom_button(__('Rebuild Variants…'), function() {
			new frappe.ui.Dialog({
				title: __('Rebuild Variants'),
				fields: [
					{
						fieldname: 'warning_html', fieldtype: 'HTML',
						options: '<div class="alert alert-warning"><strong>' + __('Warning') + ':</strong> ' +
							__('This deletes all existing variants in Shopware and recreates them from ERPNext. Run a Dry Run first.') +
							'</div>'
					},
					{ fieldname: 'batch_size', fieldtype: 'Int', label: __('Batch Size'), default: 10 },
					{ fieldname: 'sync_prices', fieldtype: 'Check', label: __('Sync Prices'), default: 1 },
					{ fieldtype: 'Section Break', label: __('Filter (optional)') },
					{ fieldname: 'item_group', fieldtype: 'Link', label: __('Item Group filter'), options: 'Item Group' },
					{ fieldtype: 'Section Break', label: __('Execution') },
					{
						fieldname: 'dry_run', fieldtype: 'Check',
						label: __('Dry Run'), default: 1,
						description: __('Preview the operation without applying.')
					}
				],
				primary_action_label: __('Start'),
				primary_action: function(values) {
					let run = () => {
						this.hide();
						frappe.call({
							method: 'ecommerce_integrations.shopware6.export.reconciliation.enqueue_force_sync_all_variants',
							args: {
								batch_size: values.batch_size || 10,
								sync_prices: values.sync_prices ? 1 : 0,
								item_group: values.item_group || null,
								dry_run: values.dry_run ? 1 : 0
							},
							callback: function(r) {
								if (r.message && r.message.success) {
									frappe.show_alert({
										message: values.dry_run
											? __('Dry run started. Check logs for results.')
											: __('Variant sync started in background.'),
										indicator: 'green'
									});
								}
							}
						});
					};
					if (!values.dry_run) {
						frappe.confirm(__('Delete and recreate all variants in Shopware?'), run);
					} else {
						run();
					}
				}
			}).show();
		}, __('Sync to Shopware'));

		frm.add_custom_button(__('Force Image Sync'), function() {
			new frappe.ui.Dialog({
				title: __('Force Image Sync'),
				fields: [
					{ fieldname: 'batch_size', fieldtype: 'Int', label: __('Batch Size'), default: 100 },
					{ fieldname: 'workers', fieldtype: 'Int', label: __('Parallel Workers'), default: 4, description: '1-8' },
					{ fieldtype: 'Section Break', label: __('Filter (optional)') },
					{ fieldname: 'item_group', fieldtype: 'Link', label: __('Item Group filter'), options: 'Item Group' },
					{
						fieldname: 'parent_item', fieldtype: 'Link', label: __('Parent Item'), options: 'Item',
						get_query: () => ({ filters: { has_variants: 1 } })
					}
				],
				primary_action_label: __('Start'),
				primary_action: function(values) {
					this.hide();
					frappe.call({
						method: 'ecommerce_integrations.shopware6.export.reconciliation.enqueue_force_sync_all_images_parallel',
						args: {
							batch_size: values.batch_size || 100,
							workers: Math.min(Math.max(values.workers || 4, 1), 8),
							item_group: values.item_group || null,
							parent_item: values.parent_item || null
						},
						callback: function(r) {
							if (r.message) {
								frappe.show_alert({
									message: r.message.message,
									indicator: r.message.success ? 'green' : 'orange'
								});
							}
						}
					});
				}
			}).show();
		}, __('Sync to Shopware'));

		// === Import from Shopware group (U3) ===
		frm.add_custom_button(__('Orders'), function() {
			new frappe.ui.Dialog({
				title: __('Import Orders from Shopware'),
				fields: [
					{
						fieldname: 'from_date', fieldtype: 'Date', label: __('From Date'), reqd: 1,
						default: frappe.datetime.add_months(frappe.datetime.get_today(), -1)
					},
					{
						fieldname: 'to_date', fieldtype: 'Date', label: __('To Date'), reqd: 1,
						default: frappe.datetime.get_today()
					},
					{ fieldname: 'limit', fieldtype: 'Int', label: __('Limit'), default: 100 }
				],
				primary_action_label: __('Import'),
				primary_action: function(values) {
					this.hide();
					frappe.call({
						method: 'ecommerce_integrations.shopware6.order.sync_orders_from_shopware',
						args: values,
						freeze: true,
						freeze_message: __('Importing orders...'),
						callback: function(r) {
							if (r.message) {
								frappe.msgprint({
									title: __('Order Import'),
									message: __('Imported: {0}, Skipped: {1}, Errors: {2}',
										[r.message.synced || 0, r.message.skipped || 0, r.message.errors || 0]),
									indicator: (r.message.errors || 0) ? 'orange' : 'green'
								});
							}
						}
					});
				}
			}).show();
		}, __('Import from Shopware'));

		frm.add_custom_button(__('Customers'), function() {
			new frappe.ui.Dialog({
				title: __('Import Customers from Shopware'),
				fields: [
					{ fieldname: 'limit', fieldtype: 'Int', label: __('Limit'), default: 100 }
				],
				primary_action_label: __('Import'),
				primary_action: function(values) {
					this.hide();
					frappe.call({
						method: 'ecommerce_integrations.shopware6.customer.sync_customers_from_shopware',
						args: values,
						freeze: true,
						freeze_message: __('Importing customers...'),
						callback: function(r) {
							if (r.message) {
								frappe.msgprint({
									title: __('Customer Import'),
									message: __('Imported: {0}, Skipped: {1}, Errors: {2}',
										[r.message.synced || 0, r.message.skipped || 0, r.message.errors || 0]),
									indicator: (r.message.errors || 0) ? 'orange' : 'green'
								});
							}
						}
					});
				}
			}).show();
		}, __('Import from Shopware'));

		frm.add_custom_button(__('Properties'), function() {
			new frappe.ui.Dialog({
				title: __('Import Properties from Shopware'),
				fields: [
					{
						fieldname: 'info_html', fieldtype: 'HTML',
						options: '<div class="alert alert-info">' +
							__('Imports property groups and options from Shopware as Item Attributes in ERPNext.') +
							'</div>'
					},
					{ fieldname: 'dry_run', fieldtype: 'Check', label: __('Dry Run'), default: 1 }
				],
				primary_action_label: __('Import'),
				primary_action: function(values) {
					this.hide();
					frappe.call({
						method: 'ecommerce_integrations.shopware6.import_handlers.property_importer.batch_import_properties',
						args: { dry_run: values.dry_run ? 1 : 0 },
						freeze: true,
						freeze_message: __('Importing properties...'),
						callback: function(r) {
							if (r.message) {
								let s = r.message.statistics || r.message;
								frappe.msgprint({
									title: __('Property Import'),
									message: __('Groups: {0}, Options: {1}, Errors: {2}', [s.groups_imported || 0, s.options_imported || 0, s.errors || 0]),
									indicator: (s.errors || 0) ? 'orange' : 'green'
								});
							}
						}
					});
				}
			}).show();
		}, __('Import from Shopware'));

		if (frm.doc.sync_inventory_from_shopware) {
			frm.add_custom_button(__('Stock'), function() {
				new frappe.ui.Dialog({
					title: __('Import Stock from Shopware'),
					fields: [
						{
							fieldname: 'info_html', fieldtype: 'HTML',
							options: '<div class="alert alert-warning">' +
								__('Imports stock levels from Shopware and creates Stock Entries.') +
								' <strong>' + __('This overwrites ERPNext stock with Shopware values.') + '</strong></div>'
						},
						{ fieldname: 'limit', fieldtype: 'Int', label: __('Limit'), default: 100 },
						{ fieldname: 'dry_run', fieldtype: 'Check', label: __('Dry Run'), default: 1 }
					],
					primary_action_label: __('Import'),
					primary_action: function(values) {
						this.hide();
						frappe.call({
							method: 'ecommerce_integrations.shopware6.import_handlers.stock_importer.import_stock_from_shopware',
							args: { limit: values.limit, dry_run: values.dry_run ? 1 : 0 },
							freeze: true,
							freeze_message: __('Importing stock...'),
							callback: function(r) {
								if (r.message) {
									frappe.msgprint({
										title: __('Stock Import'),
										message: r.message.message,
										indicator: r.message.success ? 'green' : 'red'
									});
								}
							}
						});
					}
				}).show();
			}, __('Import from Shopware'));
		}

		// === Maintenance group (U3) ===
		frm.add_custom_button(__('Remove Orphaned Products'), function() {
			new frappe.ui.Dialog({
				title: __('Remove Orphaned Products'),
				fields: [
					{
						fieldname: 'info_html', fieldtype: 'HTML',
						options: '<div class="alert alert-warning">' +
							'<strong>' + __('Warning') + ':</strong> ' +
							__('This deletes Shopware products that no longer exist in ERPNext.') + ' ' +
							__('Use Dry Run to preview first.') + '</div>'
					},
					{ fieldname: 'dry_run', fieldtype: 'Check', label: __('Dry Run'), default: 1 },
					{ fieldname: 'batch_size', fieldtype: 'Int', label: __('Batch Size'), default: 100 }
				],
				primary_action_label: __('Execute'),
				primary_action: function(values) {
					this.hide();
					if (values.dry_run) {
						frappe.call({
							method: 'ecommerce_integrations.shopware6.export.reconciliation.cleanup_orphaned_shopware_products',
							args: { dry_run: 1, batch_size: values.batch_size || 100 },
							freeze: true,
							freeze_message: __('Analyzing orphaned products...'),
							callback: function(r) {
								if (!r.message) return;
								let s = r.message.statistics || {};
								let orphaned = r.message.orphaned_products || [];
								let html = '<table class="table table-bordered">' +
									'<tr><td>' + __('Products in Shopware') + '</td><td><strong>' + (s.shopware_total || 0) + '</strong></td></tr>' +
									'<tr><td>' + __('Exist in ERPNext') + '</td><td><strong>' + (s.erpnext_exists || 0) + '</strong></td></tr>' +
									'<tr><td>' + __('Orphaned') + '</td><td><strong>' + (s.orphaned_count || 0) + '</strong></td></tr>' +
									'</table>';
								if (orphaned.length) {
									html += '<strong>' + __('Orphaned products (first 50):') + '</strong><ul>';
									orphaned.forEach(p => { html += '<li><strong>' + p.productNumber + '</strong>: ' + p.name + ' (' + p.type + ')</li>'; });
									if (s.orphaned_count > 50) html += '<li>... ' + __('and {0} more', [s.orphaned_count - 50]) + '</li>';
									html += '</ul>';
								}
								html += '<p class="alert alert-info">' + __('This was a dry run. Run without Dry Run to delete.') + '</p>';
								frappe.msgprint({ title: __('Orphaned Products Preview'), message: html, indicator: 'blue' });
							}
						});
					} else {
						frappe.confirm(__('This will permanently delete products from Shopware. Continue?'), function() {
							frappe.call({
								method: 'ecommerce_integrations.shopware6.export.reconciliation.enqueue_cleanup_orphaned_products',
								args: { dry_run: 0, batch_size: values.batch_size || 100 },
								callback: function(r) {
									if (r.message && r.message.success) {
										frappe.show_alert({ message: __('Cleanup started in background.'), indicator: 'green' });
									}
								}
							});
						});
					}
				}
			}).show();
		}, __('Maintenance'));

		frm.add_custom_button(__('Remove Orphaned Categories'), function() {
			frappe.confirm(__('Find and remove orphaned categories in Shopware?'), function() {
				frappe.call({
					method: 'ecommerce_integrations.shopware6.product_export.cleanup_orphaned_shopware_categories',
					args: { root_category: frm.doc.category_sync_root || 'Products', dry_run: false },
					freeze: true,
					freeze_message: __('Cleaning up categories...'),
					callback: function(r) {
						if (r.message) {
							let s = r.message.statistics || {};
							frappe.msgprint({
								title: __('Category Cleanup'),
								message: __('Orphaned: {0}, Deleted: {1}', [s.orphaned_count || 0, s.deleted || 0]),
								indicator: 'green'
							});
						}
					}
				});
			});
		}, __('Maintenance'));

		frm.add_custom_button(__('Analyze Conflicts'), function() {
			new frappe.ui.Dialog({
				title: __('Conflict Analysis'),
				fields: [
					{
						fieldname: 'info_html', fieldtype: 'HTML',
						options: '<div class="alert alert-info">' +
							__('Compares ERPNext with Shopware and reports all differences: prices, names, descriptions, status, missing products.') +
							'</div>'
					},
					{ fieldname: 'limit', fieldtype: 'Int', label: __('Limit'), default: 100 }
				],
				primary_action_label: __('Analyze'),
				primary_action: function(values) {
					this.hide();
					frappe.call({
						method: 'ecommerce_integrations.shopware6.sync.conflict_detector.detect_all_conflicts',
						args: { limit: values.limit },
						freeze: true,
						freeze_message: __('Analyzing...'),
						callback: function(r) {
							if (!r.message) return;
							let s = r.message;
							let html = '<table class="table table-bordered">' +
								'<tr><td>' + __('Checked') + '</td><td><strong>' + s.total_checked + '</strong></td></tr>' +
								'<tr><td>' + __('In Sync') + '</td><td><strong>' + s.in_sync + '</strong></td></tr>' +
								'<tr><td>' + __('Out of Sync') + '</td><td><strong>' + s.out_of_sync + '</strong></td></tr>' +
								'<tr><td>' + __('Missing in Shopware') + '</td><td><strong>' + s.missing_in_shopware + '</strong></td></tr>' +
								'<tr><td>' + __('Errors') + '</td><td><strong>' + s.errors + '</strong></td></tr>' +
								'</table>';
							if (s.conflicts && s.conflicts.length) {
								html += '<strong>' + __('Details (first 20):') + '</strong><ul>';
								s.conflicts.slice(0, 20).forEach(c => { html += '<li><strong>' + c.item_code + '</strong>: ' + c.summary + '</li>'; });
								if (s.conflicts.length > 20) html += '<li>... ' + __('and {0} more', [s.conflicts.length - 20]) + '</li>';
								html += '</ul>';
							}
							frappe.msgprint({ title: __('Conflict Analysis'), message: html, indicator: s.out_of_sync > 0 ? 'orange' : 'green' });
						}
					});
				}
			}).show();
		}, __('Maintenance'));

		// Test Connection — explicit success/failure feedback (U6)
		frm.add_custom_button(__('Test Connection'), function() {
			frm.call({
				method: 'test_connection',
				doc: frm.doc,
				freeze: true,
				freeze_message: __('Testing connection...'),
				callback: function(r) {
					let backend = frm.doc.shop_url || 'Shopware';
					if (r.message && (r.message.success || r.message.status === 'success')) {
						let channels = r.message.sales_channels;
						let msg = (channels !== undefined)
							? __('Connection OK – {0} sales channel(s)', [channels])
							: __('Connection OK – {0}', [backend]);
						frappe.show_alert({ message: msg, indicator: 'green' });
					} else {
						let err = (r.message && (r.message.error || r.message.message))
							|| __('Could not reach {0}. Check URL and credentials.', [backend]);
						frappe.msgprint({
							title: __('Connection failed'),
							message: err,
							indicator: 'red'
						});
					}
				}
			});
		}, __('Maintenance'));

		frm.add_custom_button(__('Clear Cache'), function() {
			frappe.call({
				method: 'ecommerce_integrations.shopware6.product_export.clear_shopware_cache',
				callback: function(r) {
					if (r.message && r.message.status === 'success') {
						frappe.show_alert({ message: __('Cache cleared.'), indicator: 'green' });
					}
				}
			});
		}, __('Maintenance'));

		if (frm.doc.enable_bulk_sync && frm.doc.enable_shopware) {
			frm.add_custom_button(__('Process Queue Now'), function() {
				frappe.call({
					method: 'ecommerce_integrations.shopware6.bulk_sync.force_process_queue',
					callback: function() {
						frappe.show_alert({ message: __('Queue processing started.'), indicator: 'green' });
						setTimeout(() => frm.reload_doc(), 2000);
					}
				});
			}, __('Maintenance'));

			frm.add_custom_button(__('Clear Queue'), function() {
				frappe.confirm(__('Clear all pending sync items?'), function() {
					frappe.call({
						method: 'ecommerce_integrations.shopware6.bulk_sync.clear_all_queues',
						callback: function() {
							frappe.show_alert({ message: __('Queues cleared.'), indicator: 'green' });
							frm.reload_doc();
						}
					});
				});
			}, __('Maintenance'));
		}
	}
});
