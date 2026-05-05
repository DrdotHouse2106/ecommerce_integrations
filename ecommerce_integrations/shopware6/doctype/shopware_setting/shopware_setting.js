// Copyright (c) 2024, Frappe and contributors
// For license information, please see license.txt

function render_smart_collections_widget(frm, backend) {
	const wrap = frm.fields_dict.smart_collections_html;
	if (!wrap || !wrap.$wrapper) return;
	wrap.$wrapper.html('<div class="text-muted">Loading Smart Collections…</div>');
	frappe.call({
		method: 'ecommerce_integrations.smart_collections.api.list_for_backend',
		args: { backend },
		callback: function(r) {
			const rows = r.message || [];
			if (!rows.length) {
				wrap.$wrapper.html(
					'<div class="text-muted small">No Smart Collections target this backend yet. ' +
					'Run the migration patch <code>migrate_item_group_channel_to_smart_collections</code> ' +
					'or create one from the Smart Collections list.</div>'
				);
				return;
			}
			const groups = {};
			rows.forEach(function(row) {
				(groups[row.sales_channel] = groups[row.sales_channel] || []).push(row);
			});
			const status_badge = function(row) {
				const map = {ok: 'green', error: 'red', pending: 'orange'};
				const colour = map[row.sync_status] || 'gray';
				return `<span class="indicator-pill ${colour}">${row.sync_status || 'pending'}</span>`;
			};
			const active_badge = function(row) {
				if (!row.is_active) return '<span class="text-muted">inactive</span>';
				if (!row.enabled) return '<span class="text-warning">disabled</span>';
				return '<span class="text-success">on</span>';
			};
			let html = '<div class="smart-collections-summary">';
			Object.keys(groups).sort().forEach(function(channel) {
				html += `<h5 style="margin-top:1em">${frappe.utils.escape_html(channel)}</h5>`;
				html += '<table class="table table-sm" style="margin-bottom:0">';
				html += '<thead><tr>'
				    + '<th>Collection</th>'
				    + '<th style="width:90px">State</th>'
				    + '<th style="width:90px">Items</th>'
				    + '<th style="width:90px">Visibility</th>'
				    + '<th style="width:120px">Status</th>'
				    + '<th style="width:160px">Last Synced</th>'
				    + '<th style="width:60px">Open</th>'
				    + '</tr></thead><tbody>';
				groups[channel].forEach(function(row) {
					const link = `/app/ecommerce-smart-collection/${encodeURIComponent(row.collection)}`;
					html += '<tr>';
					html += `<td>${frappe.utils.escape_html(row.title)}</td>`;
					html += `<td>${active_badge(row)}</td>`;
					html += `<td>${row.last_resolved_count || 0}</td>`;
					html += `<td class="small">${frappe.utils.escape_html(row.visibility || '—')}</td>`;
					html += `<td>${status_badge(row)}</td>`;
					html += `<td class="text-muted small">${row.last_synced_at ? frappe.datetime.str_to_user(row.last_synced_at) : '—'}</td>`;
					html += `<td><a href="${link}">→</a></td>`;
					html += '</tr>';
				});
				html += '</tbody></table>';
			});
			html += '</div>';
			wrap.$wrapper.html(html);
		}
	});
}


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
		render_smart_collections_widget(frm, 'Shopware');
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

		frm.add_custom_button(__('Complete Sync'), function() {
			new frappe.ui.Dialog({
				title: __('Complete Sync'),
				size: 'large',
				fields: [
					{
						fieldname: 'info_html', fieldtype: 'HTML',
						options: '<div class="alert alert-info">' +
							'<strong>Complete Sync</strong> ensures Shopware matches ERPNext. ' +
							'Uses batch API for optimal performance.</div>'
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
						label: __('Dry Run'), default: 0,
						description: __('Preview changes without applying them')
					}
				],
				primary_action_label: __('Start Sync'),
				primary_action: function(values) {
					this.hide();
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
				}
			}).show();
		}).addClass('btn-primary-dark');

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
		}, __('Sync'));

		frm.add_custom_button(__('Force Price Sync'), function() {
			frappe.confirm(__('Delete and recreate all price rules?'), function() {
				frappe.call({
					method: 'ecommerce_integrations.shopware6.export.price_handler.enqueue_force_sync_all_prices',
					args: { batch_size: 20 },
					callback: function(r) {
						if (r.message && r.message.success) {
							frappe.show_alert({ message: __('Price sync started in background.'), indicator: 'green' });
						}
					}
				});
			});
		}, __('Sync'));

		frm.add_custom_button(__('Force Image Sync'), function() {
			new frappe.ui.Dialog({
				title: __('Force Image Sync'),
				fields: [
					{ fieldname: 'batch_size', fieldtype: 'Int', label: __('Batch Size'), default: 100 },
					{ fieldname: 'workers', fieldtype: 'Int', label: __('Parallel Workers'), default: 4, description: '1-8' },
					{ fieldtype: 'Section Break', label: __('Filter (optional)') },
					{ fieldname: 'item_group', fieldtype: 'Link', label: __('Item Group'), options: 'Item Group' },
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
		}, __('Sync'));

		frm.add_custom_button(__('Force Variant Sync'), function() {
			frappe.confirm(__('Delete and recreate all variants?'), function() {
				frappe.call({
					method: 'ecommerce_integrations.shopware6.export.reconciliation.enqueue_force_sync_all_variants',
					args: { batch_size: 10, sync_prices: 1 },
					callback: function(r) {
						if (r.message && r.message.success) {
							frappe.show_alert({ message: __('Variant sync started in background.'), indicator: 'green' });
						}
					}
				});
			});
		}, __('Sync'));

		frm.add_custom_button(__('Remove Orphaned Products'), function() {
			new frappe.ui.Dialog({
				title: __('Remove Orphaned Products'),
				fields: [
					{
						fieldname: 'info_html', fieldtype: 'HTML',
						options: '<div class="alert alert-warning">' +
							'<strong>Warning:</strong> This deletes Shopware products that no longer exist in ERPNext. ' +
							'Use Dry Run to preview first.</div>'
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
									'<tr><td>Products in Shopware</td><td><strong>' + (s.shopware_total || 0) + '</strong></td></tr>' +
									'<tr><td>Exist in ERPNext</td><td><strong>' + (s.erpnext_exists || 0) + '</strong></td></tr>' +
									'<tr><td>Orphaned</td><td><strong>' + (s.orphaned_count || 0) + '</strong></td></tr>' +
									'</table>';
								if (orphaned.length) {
									html += '<strong>Orphaned products (first 50):</strong><ul>';
									orphaned.forEach(p => { html += '<li><strong>' + p.productNumber + '</strong>: ' + p.name + ' (' + p.type + ')</li>'; });
									if (s.orphaned_count > 50) html += '<li>... and ' + (s.orphaned_count - 50) + ' more</li>';
									html += '</ul>';
								}
								html += '<p class="alert alert-info">This was a dry run. Run without Dry Run to delete.</p>';
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
		}, __('Sync'));

		frm.add_custom_button(__('Analyze Conflicts'), function() {
			new frappe.ui.Dialog({
				title: __('Conflict Analysis'),
				fields: [
					{
						fieldname: 'info_html', fieldtype: 'HTML',
						options: '<div class="alert alert-info">Compares ERPNext with Shopware and reports all differences: ' +
							'prices, names, descriptions, status, missing products.</div>'
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
								'<tr><td>Checked</td><td><strong>' + s.total_checked + '</strong></td></tr>' +
								'<tr><td>In Sync</td><td><strong>' + s.in_sync + '</strong></td></tr>' +
								'<tr><td>Out of Sync</td><td><strong>' + s.out_of_sync + '</strong></td></tr>' +
								'<tr><td>Missing in Shopware</td><td><strong>' + s.missing_in_shopware + '</strong></td></tr>' +
								'<tr><td>Errors</td><td><strong>' + s.errors + '</strong></td></tr>' +
								'</table>';
							if (s.conflicts && s.conflicts.length) {
								html += '<strong>Details (first 20):</strong><ul>';
								s.conflicts.slice(0, 20).forEach(c => { html += '<li><strong>' + c.item_code + '</strong>: ' + c.summary + '</li>'; });
								if (s.conflicts.length > 20) html += '<li>... and ' + (s.conflicts.length - 20) + ' more</li>';
								html += '</ul>';
							}
							frappe.msgprint({ title: __('Conflict Analysis'), message: html, indicator: s.out_of_sync > 0 ? 'orange' : 'green' });
						}
					});
				}
			}).show();
		}, __('Sync'));

		frm.add_custom_button(__('Properties'), function() {
			new frappe.ui.Dialog({
				title: __('Import Properties from Shopware'),
				fields: [
					{
						fieldname: 'info_html', fieldtype: 'HTML',
						options: '<div class="alert alert-info">Imports property groups and options from Shopware as Item Attributes in ERPNext.</div>'
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
		}, __('Import'));

		if (frm.doc.sync_inventory_from_shopware) {
			frm.add_custom_button(__('Stock'), function() {
				new frappe.ui.Dialog({
					title: __('Import Stock from Shopware'),
					fields: [
						{
							fieldname: 'info_html', fieldtype: 'HTML',
							options: '<div class="alert alert-warning">Imports stock levels from Shopware and creates Stock Entries. ' +
								'<strong>This overwrites ERPNext stock with Shopware values.</strong></div>'
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
			}, __('Import'));
		}

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
		}, __('Import'));

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
		}, __('Import'));

		frm.add_custom_button(__('Test Connection'), function() {
			frm.call({
				method: 'test_connection',
				doc: frm.doc,
				freeze: true,
				freeze_message: __('Testing connection...')
			});
		}, __('Tools'));

		frm.add_custom_button(__('Clear Cache'), function() {
			frappe.call({
				method: 'ecommerce_integrations.shopware6.product_export.clear_shopware_cache',
				callback: function(r) {
					if (r.message && r.message.status === 'success') {
						frappe.show_alert({ message: __('Cache cleared.'), indicator: 'green' });
					}
				}
			});
		}, __('Tools'));

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
		}, __('Tools'));

		if (frm.doc.enable_bulk_sync && frm.doc.enable_shopware) {
			frm.add_custom_button(__('Process Queue Now'), function() {
				frappe.call({
					method: 'ecommerce_integrations.shopware6.bulk_sync.force_process_queue',
					callback: function() {
						frappe.show_alert({ message: __('Queue processing started.'), indicator: 'green' });
						setTimeout(() => frm.reload_doc(), 2000);
					}
				});
			}, __('Tools'));

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
			}, __('Tools'));
		}
	}
});
