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
		if (window.ecom_product_sync_widget) {
			window.ecom_product_sync_widget.render(frm, 'Shopware');
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
			const _i = window.ecom_icon || (() => '');
			const wizard_label = _i('wand-sparkles', 'sm') + ' ' + __('Setup Wizard');
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

		// Product Sync (single source of truth) — opens the dedicated
		// Ecommerce Product Sync doctype. The old "Complete Sync"
		// dialog used to live here but it duplicated what the Product
		// Sync's preview + apply pipeline already does (and badly: no
		// hash-delta detection, no background runner, no audit row).
		// We keep one big button at the top of the form so operators
		// land on the right surface from any direction.
		frm.add_custom_button(__('Open Product Sync'), function() {
			frappe.set_route('List', 'Ecommerce Product Sync', { backend: 'Shopware' });
		}).addClass('btn-primary-dark');

		// Catalog Mirror öffnen — single source of truth for category /
		// item-group → Shopware-category tree pushes (plus orphan-category
		// cleanup). The Shopware Setting page used to ship "Categories" and
		// "Remove Orphaned Categories" buttons each with their own dialog;
		// both lived in Catalog Mirror's domain so we now redirect there.
		frm.add_custom_button(__('Open Catalog Mirror'), function() {
			frappe.set_route('List', 'Ecommerce Catalog Mirror', { backend: 'Shopware' });
		}, __('Open sync'));

		// Pull Sync öffnen — single source of truth for backend → ERP
		// pulls (orders, customers, stock). The legacy "Orders" /
		// "Customers" / "Stock" / "Properties" dialogs duplicated what
		// the Pull Sync doctype now does with watermarks and audit runs.
		frm.add_custom_button(__('Open Pull Sync'), function() {
			frappe.set_route('List', 'Ecommerce Pull Sync', { backend: 'Shopware' });
		}, __('Open sync'));

		// Operational buttons (Maintenance group) — these are not sync
		// triggers, they belong on the Setting page because they act
		// on this Setting's own state (connection probe, local caches,
		// queue lifecycle). Keeping them under the same Maintenance
		// group label so the existing UI grouping stays consistent.
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
