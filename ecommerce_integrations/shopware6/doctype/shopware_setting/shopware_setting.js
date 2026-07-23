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
			const wizard_label = _i('wand-sparkles', 'sm') + ' ' + __('Einrichtungsassistent');
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
					freeze_message: __('Verkaufskanäle werden abgerufen...'),
					callback: function() { frm.reload_doc(); }
				});
			});
		}

		if (frm.is_new()) return;

		// First-time setup intro (U14)
		if (!frm.doc.shop_url) {
			frm.set_intro(
				__('Einrichtung: 1) Verbindung (Shop-URL + Client-ID/Secret). 2) Verbindung testen. 3) Verkaufskanäle aktualisieren. 4) Firma + Kunden-Standardwerte. 5) Upload erst nach einem Trockenlauf-Komplett-Sync aktivieren.'),
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
				__('{0} Sync-Fehler in den letzten 24h', [count]),
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
		frm.add_custom_button(__('Produkt-Sync öffnen'), function() {
			frappe.set_route('List', 'Ecommerce Product Sync', { backend: 'Shopware' });
		}).addClass('btn-primary-dark');

		// Catalog Mirror öffnen — single source of truth for category /
		// item-group → Shopware-category tree pushes (plus orphan-category
		// cleanup). The Shopware Setting page used to ship "Categories" and
		// "Remove Orphaned Categories" buttons each with their own dialog;
		// both lived in Catalog Mirror's domain so we now redirect there.
		frm.add_custom_button(__('Catalog Mirror öffnen'), function() {
			frappe.set_route('List', 'Ecommerce Catalog Mirror', { backend: 'Shopware' });
		}, __('Sync öffnen'));

		// One-time backfill for shops that maintained categories only in
		// Shopware so far (no matching Item Group tree exists yet).
		// Catalog Mirror's own "Adopt" flow needs the Item Group to
		// already exist — this creates it from the live Shopware tree,
		// once. Safe to re-run: matches by shopware_category_id first,
		// then by name (adopts a same-named hand-made Item Group instead
		// of duplicating it).
		frm.add_custom_button(__('Kategorien aus Shopware importieren'), function() {
			frappe.confirm(
				__('Dies erstellt/aktualisiert Item Groups in ERPNext aus eurem gesamten live Shopware-Kategoriebaum, inklusive Bilder. Erneutes Ausführen ist sicher (keine Duplikate). Der Import läuft im Hintergrund. Fortfahren?'),
				function() {
					frappe.call({
						method: 'ecommerce_integrations.shopware6.import_handlers.category_importer.import_categories_from_shopware',
						callback: function(r) {
							let s = r.message || {};
							if (s.log) {
								frappe.msgprint({
									title: __('Kategorie-Import läuft'),
									message: __('Der Import wurde im Hintergrund eingereiht. Fortschritt und Ergebnis findet ihr im Ecommerce Integration Log {0}.', [
										`<a href="/app/ecommerce-integration-log/${s.log}">${s.log}</a>`
									]),
									indicator: 'blue'
								});
							}
						}
					});
				}
			);
		}, __('Sync öffnen'));

		// Companion to the category import above: links existing Items to
		// the just-imported Item Groups based on Shopware's real
		// product→category assignment (Product.categoryIds). Never touches
		// an Item's existing item_group — only adds rows to
		// additional_item_groups, so a category from a WeClapp import is
		// never overwritten.
		frm.add_custom_button(__('Artikel-Kategorien aus Shopware verknüpfen'), function() {
			frappe.confirm(
				__('Dies ordnet bestehende Artikel den (bereits importierten) Item Groups zu, basierend auf ihrer tatsächlichen Kategorie-Zuordnung in Shopware. Die bisherige Item Group jedes Artikels bleibt unangetastet — Shopware-Kategorien werden nur zusätzlich ergänzt. Die Verknüpfung läuft im Hintergrund. Fortfahren?'),
				function() {
					frappe.call({
						method: 'ecommerce_integrations.shopware6.import_handlers.product_category_linker.link_item_categories_from_shopware',
						callback: function(r) {
							let s = r.message || {};
							if (s.log) {
								frappe.msgprint({
									title: __('Artikel-Kategorie-Verknüpfung läuft'),
									message: __('Die Verknüpfung wurde im Hintergrund eingereiht. Fortschritt und Ergebnis findet ihr im Ecommerce Integration Log {0}.', [
										`<a href="/app/ecommerce-integration-log/${s.log}">${s.log}</a>`
									]),
									indicator: 'blue'
								});
							}
						}
					});
				}
			);
		}, __('Sync öffnen'));

		// Pull Sync öffnen — single source of truth for backend → ERP
		// pulls (orders, customers, stock). The legacy "Orders" /
		// "Customers" / "Stock" / "Properties" dialogs duplicated what
		// the Pull Sync doctype now does with watermarks and audit runs.
		frm.add_custom_button(__('Pull Sync öffnen'), function() {
			frappe.set_route('List', 'Ecommerce Pull Sync', { backend: 'Shopware' });
		}, __('Sync öffnen'));

		// Operational buttons (Maintenance group) — these are not sync
		// triggers, they belong on the Setting page because they act
		// on this Setting's own state (connection probe, local caches,
		// queue lifecycle). Keeping them under the same Maintenance
		// group label so the existing UI grouping stays consistent.
		frm.add_custom_button(__('Verbindung testen'), function() {
			frm.call({
				method: 'test_connection',
				doc: frm.doc,
				freeze: true,
				freeze_message: __('Verbindung wird getestet...'),
				callback: function(r) {
					let backend = frm.doc.shop_url || 'Shopware';
					if (r.message && (r.message.success || r.message.status === 'success')) {
						let channels = r.message.sales_channels;
						let msg = (channels !== undefined)
							? __('Verbindung OK – {0} Verkaufskanal/-kanäle', [channels])
							: __('Verbindung OK – {0}', [backend]);
						frappe.show_alert({ message: msg, indicator: 'green' });
					} else {
						let err = (r.message && (r.message.error || r.message.message))
							|| __('{0} nicht erreichbar. URL und Zugangsdaten prüfen.', [backend]);
						frappe.msgprint({
							title: __('Verbindung fehlgeschlagen'),
							message: err,
							indicator: 'red'
						});
					}
				}
			});
		}, __('Wartung'));

		frm.add_custom_button(__('Cache leeren'), function() {
			frappe.call({
				method: 'ecommerce_integrations.shopware6.product_export.clear_shopware_cache',
				callback: function(r) {
					if (r.message && r.message.status === 'success') {
						frappe.show_alert({ message: __('Cache geleert.'), indicator: 'green' });
					}
				}
			});
		}, __('Wartung'));
	}
});
