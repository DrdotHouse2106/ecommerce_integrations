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

		if (frm.is_new()) return;

		frm.add_custom_button(__('Import Orders'), function() {
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
						callback: function(r) {
							frappe.show_alert({ message: __('Order import completed. Check logs for details.'), indicator: 'green' });
						}
					});
				}
			}).show();
		}, __('Import'));

		frm.add_custom_button(__('Import Customers'), function() {
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
