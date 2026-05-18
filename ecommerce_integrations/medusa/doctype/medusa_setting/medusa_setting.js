// Smart Collections widget (render + Add dialog) is loaded via doctype_js
// from public/js/smart_collections/setting_widget.js — exposes
// window.smart_collections_widget.render(frm, backend).

frappe.ui.form.on('Medusa Setting', {
	onload: function(frm) {
		if (frm.doc.__onload) {
			['sales_order_series', 'delivery_note_series'].forEach(field => {
				let key = field + '_options';
				if (frm.doc.__onload[key]) {
					frm.set_df_property(field, 'options', frm.doc.__onload[key].split('\n'));
				}
			});
		}
	},

	refresh: function(frm) {
		if (window.catalog_mirror_widget) {
			window.catalog_mirror_widget.render(frm, 'Medusa');
		}
		if (window.smart_collections_widget) {
			window.smart_collections_widget.render(frm, 'Medusa');
		}
		if (window.ecom_product_sync_widget) {
			window.ecom_product_sync_widget.render(frm, 'Medusa');
		}
		// Track A UX surfaces — health banner, safety mode, webhook widget,
		// setup wizard. All four are no-ops on new (unsaved) forms.
		if (window.ecom_health_banner) {
			window.ecom_health_banner.render(frm, 'Medusa');
		}
		if (window.ecom_safety_mode) {
			window.ecom_safety_mode.attach_button(frm, 'Medusa');
		}
		if (window.ecom_webhook_widget) {
			window.ecom_webhook_widget.render(frm, 'Medusa');
		}
		if (window.medusa_setup_wizard && !frm.is_new()) {
			const _i = window.ecom_icon || (() => '');
			const wizard_label = _i('wand-sparkles', 'sm') + ' ' + __('Setup Wizard');
			if (!frm.custom_buttons || !frm.custom_buttons[wizard_label]) {
				frm.add_custom_button(wizard_label, function() {
					window.medusa_setup_wizard.open(frm);
				}).addClass('btn-primary');
			}
		}
		if (frm.fields_dict.fetch_sales_channels_btn && frm.fields_dict.fetch_sales_channels_btn.$input) {
			frm.fields_dict.fetch_sales_channels_btn.$input.off('click').on('click', function() {
				frm.call({
					method: 'fetch_sales_channels',
					doc: frm.doc,
					freeze: true,
					freeze_message: __('Fetching Sales Channels from Medusa...'),
					callback: function() { frm.reload_doc(); }
				});
			});
		}

		if (frm.fields_dict.fetch_stock_locations_btn && frm.fields_dict.fetch_stock_locations_btn.$input) {
			frm.fields_dict.fetch_stock_locations_btn.$input.off('click').on('click', function() {
				frm.call({
					method: 'fetch_stock_locations',
					doc: frm.doc,
					freeze: true,
					freeze_message: __('Syncing Stock Locations with Medusa...'),
					callback: function() { frm.reload_doc(); }
				});
			});
		}

		if (frm.is_new()) return;

		// First-time setup intro (U14)
		if (!frm.doc.medusa_url) {
			frm.set_intro(
				__('Setup: 1) Connection (Medusa URL + API Key). 2) Test Connection. 3) Refresh Sales Channels. 4) Company + Customer defaults. 5) Enable Upload only after a Dry-Run Complete Sync.'),
				'blue'
			);
		}

		// Health dashboard indicator (U11) — error count in last 24h for medusa
		frappe.db.count('Ecommerce Integration Log', {
			filters: {
				integration: 'medusa',
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
							__('pushes all ERPNext products, categories and prices to Medusa. Runs as a background job.') +
							'</div>'
					},
					{ fieldtype: 'Section Break', label: __('Options') },
					{ fieldname: 'sync_categories', fieldtype: 'Check', label: __('Sync Categories'), default: 1 },
					{ fieldname: 'sync_products', fieldtype: 'Check', label: __('Sync Products'), default: 1 },
					{ fieldtype: 'Column Break' },
					{ fieldname: 'sync_prices', fieldtype: 'Check', label: __('Sync Prices'), default: 1 },
					{
						fieldname: 'sync_stock', fieldtype: 'Check', label: __('Sync Stock'), default: 0,
						description: __('Overwrites Medusa inventory with ERPNext values')
					},
					{ fieldtype: 'Section Break', label: __('Execution') },
					{
						fieldname: 'batch_size', fieldtype: 'Int', label: __('Batch Size'),
						default: frm.doc.product_batch_size || 50
					},
					{
						fieldname: 'dry_run', fieldtype: 'Check', label: __('Dry Run'), default: 1,
						description: __('Preview changes without applying them. Recommended for first run.')
					}
				],
				primary_action_label: __('Preview Changes'),
				primary_action: function(values) {
					let run_sync = function() {
						dlg.hide();
						frappe.call({
							method: 'ecommerce_integrations.medusa.product_export.enqueue_full_sync',
							args: values,
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
								[frm.doc.medusa_url || 'Medusa']),
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

		// === Sync to Medusa group (U3) ===
		frm.add_custom_button(__('Categories'), function() {
			new frappe.ui.Dialog({
				title: __('Sync Categories'),
				fields: [
					{
						fieldname: 'category_root', fieldtype: 'Link',
						label: __('Root Category'), options: 'Item Group',
						default: frm.doc.category_sync_root, reqd: 1
					},
					{ fieldname: 'dry_run', fieldtype: 'Check', label: __('Dry Run'), default: 1 }
				],
				primary_action_label: __('Sync'),
				primary_action: function(values) {
					this.hide();
					frappe.call({
						method: 'ecommerce_integrations.medusa.product_export.sync_categories',
						args: values,
						freeze: true,
						freeze_message: __('Syncing categories...'),
						callback: function(r) {
							if (r.message) {
								let s = r.message;
								frappe.msgprint({
									title: __('Category Sync'),
									message: __('Total: {0}, Synced: {1}, Errors: {2}',
										[s.total || 0, s.synced || 0, s.errors || 0]),
									indicator: (s.errors || 0) ? 'orange' : 'green'
								});
							}
						}
					});
				}
			}).show();
		}, __('Sync to Medusa'));

		// Rebuild Prices (U2) — was "Force Price Sync"
		frm.add_custom_button(__('Rebuild Prices…'), function() {
			new frappe.ui.Dialog({
				title: __('Rebuild Prices'),
				fields: [
					{
						fieldname: 'warning_html', fieldtype: 'HTML',
						options: '<div class="alert alert-warning"><strong>' + __('Warning') + ':</strong> ' +
							__('This deletes all existing price rules in Medusa and recreates them from ERPNext. Run a Dry Run first.') +
							'</div>'
					},
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
							method: 'ecommerce_integrations.medusa.product_export.enqueue_force_price_sync',
							args: {
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
						frappe.confirm(__('Delete and recreate all price rules in Medusa?'), run);
					} else {
						run();
					}
				}
			}).show();
		}, __('Sync to Medusa'));

		// === Import from Medusa group (U3) ===
		// U7: replaced misleading from_date/to_date/limit dialog with simple confirm,
		// because medusa.order.scheduled_sync.sync_new_orders ignores those args.
		frm.add_custom_button(__('Orders'), function() {
			frappe.confirm(
				__('Import new Medusa orders since the last sync ({0})?',
					[frm.doc.last_order_sync || __('never')]),
				function() {
					frappe.call({
						method: 'ecommerce_integrations.medusa.order.scheduled_sync.sync_new_orders',
						freeze: true,
						freeze_message: __('Importing orders...'),
						callback: function() {
							frappe.show_alert({ message: __('Order import completed. Check logs for details.'), indicator: 'green' });
						}
					});
				}
			);
		}, __('Import from Medusa'));

		frm.add_custom_button(__('Customers'), function() {
			frappe.confirm(
				__('Import new Medusa customers since the last sync ({0})?',
					[frm.doc.last_customer_sync || __('never')]),
				function() {
					frappe.call({
						method: 'ecommerce_integrations.medusa.scheduled_customer_sync.sync_new_customers',
						freeze: true,
						freeze_message: __('Importing customers...'),
						callback: function() {
							frappe.show_alert({ message: __('Customer import completed. Check logs for details.'), indicator: 'green' });
						}
					});
				}
			);
		}, __('Import from Medusa'));

		// === Maintenance group (U3) ===
		// Test Connection — explicit success/failure feedback (U6)
		frm.add_custom_button(__('Test Connection'), function() {
			frm.call({
				method: 'test_connection',
				doc: frm.doc,
				freeze: true,
				freeze_message: __('Testing connection...'),
				callback: function(r) {
					let backend = frm.doc.medusa_url || 'Medusa';
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
	}
});
