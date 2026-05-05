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
		if (window.smart_collections_widget) {
			window.smart_collections_widget.render(frm, 'Medusa');
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

		frm.add_custom_button(__('Complete Sync'), function() {
			new frappe.ui.Dialog({
				title: __('Complete Sync'),
				size: 'large',
				fields: [
					{
						fieldname: 'info_html', fieldtype: 'HTML',
						options: '<div class="alert alert-info">' +
							'<strong>Complete Sync</strong> pushes all ERPNext products, categories and prices to Medusa. ' +
							'Runs as a background job.</div>'
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
						fieldname: 'dry_run', fieldtype: 'Check', label: __('Dry Run'), default: 0,
						description: __('Preview changes without applying them')
					}
				],
				primary_action_label: __('Start Sync'),
				primary_action: function(values) {
					this.hide();
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
		}, __('Sync'));

		frm.add_custom_button(__('Force Price Sync'), function() {
			frappe.confirm(__('Re-push all prices to Medusa?'), function() {
				frappe.call({
					method: 'ecommerce_integrations.medusa.product_export.enqueue_force_price_sync',
					callback: function(r) {
						if (r.message && r.message.success) {
							frappe.show_alert({ message: __('Price sync started in background.'), indicator: 'green' });
						}
					}
				});
			});
		}, __('Sync'));

		frm.add_custom_button(__('Orders'), function() {
			new frappe.ui.Dialog({
				title: __('Import Orders from Medusa'),
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
						method: 'ecommerce_integrations.medusa.order.scheduled_sync.sync_new_orders',
						freeze: true,
						freeze_message: __('Importing orders...'),
						callback: function() {
							frappe.show_alert({ message: __('Order import completed. Check logs for details.'), indicator: 'green' });
						}
					});
				}
			}).show();
		}, __('Import'));

		frm.add_custom_button(__('Customers'), function() {
			frappe.call({
				method: 'ecommerce_integrations.medusa.scheduled_customer_sync.sync_new_customers',
				freeze: true,
				freeze_message: __('Importing customers...'),
				callback: function() {
					frappe.show_alert({ message: __('Customer import completed. Check logs for details.'), indicator: 'green' });
				}
			});
		}, __('Import'));

		frm.add_custom_button(__('Test Connection'), function() {
			frm.call({
				method: 'test_connection',
				doc: frm.doc,
				freeze: true,
				freeze_message: __('Testing connection...')
			});
		}, __('Tools'));
	}
});
