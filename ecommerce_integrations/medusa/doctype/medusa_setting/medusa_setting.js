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
		// Product Sync öffnen — single source of truth for ERP → Medusa
		// pushes. The legacy "Complete Sync" dialog used to live here
		// but it duplicated what the Product Sync's preview + apply
		// pipeline already does, without hash-delta detection or audit.
		frm.add_custom_button(__('Open Product Sync'), function() {
			frappe.set_route('List', 'Ecommerce Product Sync', { backend: 'Medusa' });
		}).addClass('btn-primary-dark');

		// Catalog Mirror öffnen — single source of truth for category
		// pushes. Replaces the per-channel "Categories" sync dialog.
		frm.add_custom_button(__('Open Catalog Mirror'), function() {
			frappe.set_route('List', 'Ecommerce Catalog Mirror', { backend: 'Medusa' });
		}, __('Open sync'));

		// Pull Sync öffnen — single source of truth for backend → ERP
		// pulls (orders, customers). Replaces the legacy "Orders" /
		// "Customers" buttons.
		frm.add_custom_button(__('Open Pull Sync'), function() {
			frappe.set_route('List', 'Ecommerce Pull Sync', { backend: 'Medusa' });
		}, __('Open sync'));


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
