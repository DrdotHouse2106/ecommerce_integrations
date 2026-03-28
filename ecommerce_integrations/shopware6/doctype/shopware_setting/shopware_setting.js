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

		if (!frm.is_new()) {
			// =====================================================
			// MAIN BUTTON: Complete Sync (No-Brainer)
			// This is THE button for guaranteed sync
			// =====================================================
			frm.add_custom_button(__('🔄 Complete Sync'), function() {
				let dialog = new frappe.ui.Dialog({
					title: __('Complete Sync - ERPNext → Shopware'),
					size: 'large',
					fields: [
						{
							fieldname: 'info_html',
							fieldtype: 'HTML',
							options: `<div class="alert alert-success">
								<p><strong>✅ Complete Sync</strong> macht Shopware 100% identisch zu ERPNext.</p>
								<p class="text-muted small">Nutzt Batch-API für optimale Performance (5-10x schneller).</p>
							</div>`
						},
						{
							fieldtype: 'Section Break',
							label: __('Sync-Optionen'),
							collapsible: 0
						},
						{
							fieldname: 'sync_images',
							fieldtype: 'Check',
							label: __('Bilder synchronisieren'),
							default: 1,
							description: __('Alle Produktbilder nach Shopware hochladen')
						},
						{
							fieldname: 'sync_stock',
							fieldtype: 'Check',
							label: __('Lagerbestand synchronisieren'),
							default: 0,
							description: __('⚠️ Überschreibt Shopware-Bestand mit ERPNext-Werten')
						},
						{
							fieldtype: 'Column Break'
						},
						{
							fieldname: 'category_root',
							fieldtype: 'Link',
							label: __('Root Kategorie'),
							options: 'Item Group',
							default: frm.doc.category_sync_root || 'Products',
							description: __('Nur Kategorien unter diesem Root')
						},
						{
							fieldname: 'cleanup_orphans',
							fieldtype: 'Check',
							label: __('Verwaiste Einträge bereinigen'),
							default: 1,
							description: __('Kategorien, Varianten, Properties die nicht mehr in ERPNext existieren')
						},
						{
							fieldtype: 'Section Break',
							label: __('Phasen überspringen'),
							collapsible: 1,
							collapsed: 1
						},
						{
							fieldname: 'skip_categories',
							fieldtype: 'Check',
							label: __('Kategorien überspringen'),
							default: 0
						},
						{
							fieldname: 'skip_templates',
							fieldtype: 'Check',
							label: __('Templates überspringen'),
							default: 0,
							description: __('Parent-Produkte mit Varianten überspringen')
						},
						{
							fieldname: 'skip_products',
							fieldtype: 'Check',
							label: __('Produkte überspringen'),
							default: 0
						},
						{
							fieldtype: 'Column Break'
						},
						{
							fieldname: 'skip_variants',
							fieldtype: 'Check',
							label: __('Varianten überspringen'),
							default: 0
						},
						{
							fieldname: 'skip_prices',
							fieldtype: 'Check',
							label: __('Preiskorrektur überspringen'),
							default: 0
						},
						{
							fieldtype: 'Section Break',
							label: __('Ausführung')
						},
						{
							fieldname: 'dry_run',
							fieldtype: 'Check',
							label: __('Nur Vorschau (Dry Run)'),
							default: 0,
							description: __('Zeigt was geändert werden würde, ohne es zu tun')
						}
					],
					primary_action_label: __('Sync starten'),
					primary_action: function(values) {
						dialog.hide();
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
											? __('Dry-Run gestartet - prüfe Shopware Logs für Ergebnisse')
											: __('Complete Sync gestartet im Hintergrund'),
										indicator: 'green'
									});
								} else {
									frappe.msgprint({
										title: __('Fehler'),
										message: r.message?.message || __('Unbekannter Fehler'),
										indicator: 'red'
									});
								}
							}
						});
					}
				});
				dialog.show();
			}).addClass('btn-primary-dark');

			// =====================================================
			// SYNC DROPDOWN: Einzelne Sync-Optionen
			// =====================================================

			// Categories Only
			frm.add_custom_button(__('Kategorien'), function() {
				let dialog = new frappe.ui.Dialog({
					title: __('Kategorien synchronisieren'),
					fields: [
						{
							fieldname: 'category_root',
							fieldtype: 'Link',
							label: __('Root Kategorie'),
							options: 'Item Group',
							default: frm.doc.category_sync_root || 'Products',
							reqd: 1
						},
						{
							fieldname: 'skip_root',
							fieldtype: 'Check',
							label: __('Root überspringen'),
							default: 1
						},
						{
							fieldname: 'dry_run',
							fieldtype: 'Check',
							label: __('Dry Run'),
							default: 1
						}
					],
					primary_action_label: __('Sync'),
					primary_action: function(values) {
						dialog.hide();
						frappe.call({
							method: 'ecommerce_integrations.shopware6.product_export.sync_all_categories_to_shopware',
							args: values,
							freeze: true,
							freeze_message: __('Synchronisiere Kategorien...'),
							callback: function(r) {
								if (r.message) {
									let stats = r.message.statistics || {};
									frappe.msgprint({
										title: __('Kategorie-Sync'),
										message: `Gesamt: ${stats.total || 0}, Synchronisiert: ${stats.synced || 0}, Fehler: ${(stats.errors || []).length}`,
										indicator: (stats.errors || []).length > 0 ? 'orange' : 'green'
									});
								}
							}
						});
					}
				});
				dialog.show();
			}, __('Sync'));

			// Force Price Sync
			frm.add_custom_button(__('Preise (Force)'), function() {
				frappe.confirm(
					__('Alle Preisregeln löschen und neu erstellen?'),
					function() {
						frappe.call({
							method: 'ecommerce_integrations.shopware6.export.price_handler.enqueue_force_sync_all_prices',
							args: { batch_size: 20 },
							callback: function(r) {
								if (r.message && r.message.success) {
									frappe.show_alert({
										message: __('Force Price Sync gestartet'),
										indicator: 'green'
									});
								}
							}
						});
					}
				);
			}, __('Sync'));

			// Force Image Sync (Parallel)
			frm.add_custom_button(__('Bilder (Force)'), function() {
				let d = new frappe.ui.Dialog({
					title: __('Force Image Sync'),
					fields: [
						{
							label: __('Batch Größe'),
							fieldname: 'batch_size',
							fieldtype: 'Int',
							default: 100,
							description: __('Produkte pro Batch')
						},
						{
							label: __('Parallele Worker'),
							fieldname: 'workers',
							fieldtype: 'Int',
							default: 4,
							description: __('1-8 Worker für parallele Verarbeitung')
						},
						{
							fieldtype: 'Section Break',
							label: __('Filter (optional)')
						},
						{
							label: __('Artikelgruppe'),
							fieldname: 'item_group',
							fieldtype: 'Link',
							options: 'Item Group',
							description: __('Nur Artikel dieser Gruppe')
						},
						{
							label: __('Parent Artikel'),
							fieldname: 'parent_item',
							fieldtype: 'Link',
							options: 'Item',
							get_query: function() {
								return { filters: { has_variants: 1 } };
							},
							description: __('Nur Varianten dieses Parents')
						}
					],
					primary_action_label: __('Starten'),
					primary_action: function(values) {
						frappe.call({
							method: 'ecommerce_integrations.shopware6.export.reconciliation.enqueue_force_sync_all_images_parallel',
							args: {
								batch_size: values.batch_size || 100,
								workers: Math.min(Math.max(values.workers || 4, 1), 8),
								item_group: values.item_group || null,
								parent_item: values.parent_item || null
							},
							callback: function(r) {
								if (r.message && r.message.success) {
									frappe.show_alert({
										message: r.message.message,
										indicator: 'green'
									});
								} else if (r.message) {
									frappe.show_alert({
										message: r.message.message,
										indicator: 'orange'
									});
								}
							}
						});
						d.hide();
					}
				});
				d.show();
			}, __('Sync'));

			// Force Variant Sync
			frm.add_custom_button(__('Varianten (Force)'), function() {
				frappe.confirm(
					__('Alle Varianten löschen und neu erstellen?'),
					function() {
						frappe.call({
							method: 'ecommerce_integrations.shopware6.export.reconciliation.enqueue_force_sync_all_variants',
							args: { batch_size: 10, sync_prices: 1 },
							callback: function(r) {
								if (r.message && r.message.success) {
									frappe.show_alert({
										message: __('Force Variant Sync gestartet'),
										indicator: 'green'
									});
								}
							}
						});
					}
				);
			}, __('Sync'));

			// Cleanup Orphaned Products in Shopware
			frm.add_custom_button(__('Verwaiste Produkte löschen'), function() {
				let dialog = new frappe.ui.Dialog({
					title: __('Verwaiste Produkte in Shopware löschen'),
					fields: [
						{
							fieldname: 'info_html',
							fieldtype: 'HTML',
							options: `<div class="alert alert-danger">
								<p><strong>⚠️ Achtung:</strong> Diese Funktion löscht Produkte in Shopware, die in ERPNext nicht mehr existieren!</p>
								<ul>
									<li>Findet alle Produkte in Shopware</li>
									<li>Prüft ob die Artikelnummer in ERPNext existiert</li>
									<li>Löscht Produkte die in ERPNext nicht gefunden werden</li>
								</ul>
								<p class="text-muted">Dry-Run zeigt Vorschau sofort, echtes Löschen läuft im Hintergrund.</p>
							</div>`
						},
						{
							fieldname: 'dry_run',
							fieldtype: 'Check',
							label: __('Dry Run (nur Vorschau)'),
							default: 1,
							description: __('Zeigt was gelöscht werden würde, ohne es zu tun')
						},
						{
							fieldname: 'batch_size',
							fieldtype: 'Int',
							label: __('Batch Size'),
							default: 100,
							description: __('Produkte pro API-Aufruf')
						}
					],
					primary_action_label: __('Ausführen'),
					primary_action: function(values) {
						dialog.hide();

						if (values.dry_run) {
							// Dry Run: synchron für direkte Vorschau
							frappe.call({
								method: 'ecommerce_integrations.shopware6.export.reconciliation.cleanup_orphaned_shopware_products',
								args: {
									dry_run: 1,
									batch_size: values.batch_size || 100
								},
								freeze: true,
								freeze_message: __('Analysiere verwaiste Produkte...'),
								callback: function(r) {
									if (r.message) {
										let stats = r.message.statistics || {};
										let orphaned = r.message.orphaned_products || [];

										let html = `
											<h5>Zusammenfassung</h5>
											<table class="table table-bordered">
												<tr><td>Produkte in Shopware</td><td><strong>${stats.shopware_total || 0}</strong></td></tr>
												<tr><td>Existieren in ERPNext</td><td><strong>${stats.erpnext_exists || 0}</strong></td></tr>
												<tr><td>⚠️ Verwaist (nicht in ERPNext)</td><td><strong>${stats.orphaned_count || 0}</strong></td></tr>
											</table>
										`;

										if (orphaned.length > 0) {
											html += '<h5>Verwaiste Produkte (erste 50)</h5><ul>';
											orphaned.forEach(p => {
												html += `<li><strong>${p.productNumber}</strong>: ${p.name} <span class="text-muted">(${p.type})</span></li>`;
											});
											if (stats.orphaned_count > 50) {
												html += `<li>... und ${stats.orphaned_count - 50} weitere</li>`;
											}
											html += '</ul>';
										}

										html += '<p class="alert alert-info">Dies war nur eine Vorschau (Dry Run). Führe ohne Dry Run aus um die Produkte zu löschen.</p>';

										frappe.msgprint({
											title: __('Vorschau: Verwaiste Produkte'),
											message: html,
											indicator: 'blue'
										});
									}
								}
							});
						} else {
							// Echtes Löschen: Background Job
							frappe.confirm(
								__('Sind Sie sicher? Diese Aktion löscht Produkte unwiderruflich aus Shopware! Der Vorgang läuft im Hintergrund.'),
								function() {
									frappe.call({
										method: 'ecommerce_integrations.shopware6.export.reconciliation.enqueue_cleanup_orphaned_products',
										args: {
											dry_run: 0,
											batch_size: values.batch_size || 100
										},
										callback: function(r) {
											if (r.message && r.message.success) {
												frappe.show_alert({
													message: __('Cleanup gestartet im Hintergrund. Prüfe Shopware Logs für Ergebnis.'),
													indicator: 'green'
												});
											} else {
												frappe.msgprint({
													title: __('Fehler'),
													message: r.message?.message || __('Unbekannter Fehler'),
													indicator: 'red'
												});
											}
										}
									});
								}
							);
						}
					}
				});
				dialog.show();
			}, __('Sync'));

			// =====================================================
			// ANALYZE: Conflict Detection & Analysis
			// =====================================================
			frm.add_custom_button(__('Konflikte analysieren'), function() {
				let dialog = new frappe.ui.Dialog({
					title: __('Konflikt-Analyse'),
					fields: [
						{
							fieldname: 'info_html',
							fieldtype: 'HTML',
							options: `<div class="alert alert-info">
								<p>Vergleicht ERPNext mit Shopware und zeigt alle Unterschiede:</p>
								<ul>
									<li>Preisunterschiede</li>
									<li>Namens-/Beschreibungsunterschiede</li>
									<li>Status-Unterschiede (aktiv/inaktiv)</li>
									<li>Fehlende/verwaiste Produkte</li>
								</ul>
							</div>`
						},
						{
							fieldname: 'limit',
							fieldtype: 'Int',
							label: __('Limit'),
							default: 100
						}
					],
					primary_action_label: __('Analysieren'),
					primary_action: function(values) {
						dialog.hide();
						frappe.call({
							method: 'ecommerce_integrations.shopware6.sync.conflict_detector.detect_all_conflicts',
							args: { limit: values.limit },
							freeze: true,
							freeze_message: __('Analysiere Konflikte...'),
							callback: function(r) {
								if (r.message) {
									let stats = r.message;
									let html = `
										<h5>Zusammenfassung</h5>
										<table class="table table-bordered">
											<tr><td>Geprüft</td><td><strong>${stats.total_checked}</strong></td></tr>
											<tr><td>✅ Synchron</td><td><strong>${stats.in_sync}</strong></td></tr>
											<tr><td>⚠️ Unterschiede</td><td><strong>${stats.out_of_sync}</strong></td></tr>
											<tr><td>❌ Fehlt in Shopware</td><td><strong>${stats.missing_in_shopware}</strong></td></tr>
											<tr><td>🔴 Fehler</td><td><strong>${stats.errors}</strong></td></tr>
										</table>
									`;
									if (stats.conflicts && stats.conflicts.length > 0) {
										html += '<h5>Details (erste 20)</h5><ul>';
										stats.conflicts.slice(0, 20).forEach(c => {
											html += `<li><strong>${c.item_code}</strong>: ${c.summary}</li>`;
										});
										if (stats.conflicts.length > 20) {
											html += `<li>... und ${stats.conflicts.length - 20} weitere</li>`;
										}
										html += '</ul>';
									}
									frappe.msgprint({
										title: __('Konflikt-Analyse'),
										message: html,
										indicator: stats.out_of_sync > 0 ? 'orange' : 'green'
									});
								}
							}
						});
					}
				});
				dialog.show();
			}, __('Analyse'));

			// =====================================================
			// IMPORT FROM SHOPWARE (if enabled)
			// =====================================================

			// Property Import (always show - imports property definitions)
			frm.add_custom_button(__('Properties importieren'), function() {
				let dialog = new frappe.ui.Dialog({
					title: __('Properties von Shopware importieren'),
					fields: [
						{
							fieldname: 'info_html',
							fieldtype: 'HTML',
							options: `<div class="alert alert-info">
								<p>Importiert Property-Gruppen und Optionen von Shopware nach ERPNext.</p>
								<p>Erstellt Item Attributes basierend auf Shopware Properties.</p>
							</div>`
						},
						{
							fieldname: 'dry_run',
							fieldtype: 'Check',
							label: __('Dry Run'),
							default: 1
						}
					],
					primary_action_label: __('Importieren'),
					primary_action: function(values) {
						dialog.hide();
						frappe.call({
							method: 'ecommerce_integrations.shopware6.import_handlers.property_importer.batch_import_properties',
							args: { dry_run: values.dry_run ? 1 : 0 },
							freeze: true,
							freeze_message: __('Importiere Properties...'),
							callback: function(r) {
								if (r.message) {
									let stats = r.message.statistics || r.message;
									frappe.msgprint({
										title: __('Property Import'),
										message: `Gruppen: ${stats.groups_imported || 0}, Optionen: ${stats.options_imported || 0}, Fehler: ${stats.errors || 0}`,
										indicator: (stats.errors || 0) > 0 ? 'orange' : 'green'
									});
								}
							}
						});
					}
				});
				dialog.show();
			}, __('Import'));

			// Stock Import (if enabled in settings)
			if (frm.doc.sync_inventory_from_shopware) {
				frm.add_custom_button(__('Lagerbestand importieren'), function() {
					let dialog = new frappe.ui.Dialog({
						title: __('Lagerbestand von Shopware importieren'),
						fields: [
							{
								fieldname: 'info_html',
								fieldtype: 'HTML',
								options: `<div class="alert alert-warning">
									<p>Importiert Lagerbestände von Shopware und erstellt Stock Entries.</p>
									<p><strong>Achtung:</strong> Überschreibt ERPNext-Bestände mit Shopware-Werten!</p>
								</div>`
							},
							{
								fieldname: 'limit',
								fieldtype: 'Int',
								label: __('Limit'),
								default: 100
							},
							{
								fieldname: 'dry_run',
								fieldtype: 'Check',
								label: __('Dry Run'),
								default: 1
							}
						],
						primary_action_label: __('Importieren'),
						primary_action: function(values) {
							dialog.hide();
							frappe.call({
								method: 'ecommerce_integrations.shopware6.import_handlers.stock_importer.import_stock_from_shopware',
								args: {
									limit: values.limit,
									dry_run: values.dry_run ? 1 : 0
								},
								freeze: true,
								freeze_message: __('Importiere Lagerbestand...'),
								callback: function(r) {
									if (r.message) {
										if (r.message.success) {
											frappe.msgprint({
												title: __('Stock Import'),
												message: r.message.message,
												indicator: 'green'
											});
										} else {
											frappe.msgprint({
												title: __('Stock Import'),
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
				}, __('Import'));
			}

			// =====================================================
			// TOOLS: Utilities & Maintenance
			// =====================================================

			// Test Connection
			frm.add_custom_button(__('Verbindung testen'), function() {
				frm.call({
					method: 'test_connection',
					doc: frm.doc,
					freeze: true,
					freeze_message: __('Teste Verbindung...')
				});
			}, __('Tools'));

			// Clear Cache
			frm.add_custom_button(__('Cache leeren'), function() {
				frappe.call({
					method: 'ecommerce_integrations.shopware6.product_export.clear_shopware_cache',
					callback: function(r) {
						if (r.message && r.message.status === 'success') {
							frappe.show_alert({
								message: __('Cache geleert'),
								indicator: 'green'
							});
						}
					}
				});
			}, __('Tools'));

			// Cleanup Orphaned Categories
			frm.add_custom_button(__('Verwaiste Kategorien löschen'), function() {
				frappe.confirm(
					__('Verwaiste Kategorien in Shopware finden und löschen?'),
					function() {
						frappe.call({
							method: 'ecommerce_integrations.shopware6.product_export.cleanup_orphaned_shopware_categories',
							args: {
								root_category: frm.doc.category_sync_root || 'Products',
								dry_run: false
							},
							freeze: true,
							freeze_message: __('Bereinige verwaiste Kategorien...'),
							callback: function(r) {
								if (r.message) {
									let stats = r.message.statistics || {};
									frappe.msgprint({
										title: __('Cleanup'),
										message: `Verwaist: ${stats.orphaned_count || 0}, Gelöscht: ${stats.deleted || 0}`,
										indicator: 'green'
									});
								}
							}
						});
					}
				);
			}, __('Tools'));

			// =====================================================
			// ORDERS & CUSTOMERS: Data Import
			// =====================================================

			// Sync Old Orders
			frm.add_custom_button(__('Bestellungen synchronisieren'), function() {
				let dialog = new frappe.ui.Dialog({
					title: __('Bestellungen von Shopware importieren'),
					fields: [
						{
							fieldname: 'from_date',
							fieldtype: 'Date',
							label: __('Von Datum'),
							reqd: 1,
							default: frappe.datetime.add_months(frappe.datetime.get_today(), -1)
						},
						{
							fieldname: 'to_date',
							fieldtype: 'Date',
							label: __('Bis Datum'),
							reqd: 1,
							default: frappe.datetime.get_today()
						},
						{
							fieldname: 'limit',
							fieldtype: 'Int',
							label: __('Limit'),
							default: 100
						}
					],
					primary_action_label: __('Importieren'),
					primary_action: function(values) {
						dialog.hide();
						frappe.call({
							method: 'ecommerce_integrations.shopware6.order.sync_orders_from_shopware',
							args: values,
							freeze: true,
							freeze_message: __('Importiere Bestellungen...'),
							callback: function(r) {
								if (r.message) {
									frappe.msgprint({
										title: __('Bestellungs-Import'),
										message: __('Importiert: {0}, Übersprungen: {1}, Fehler: {2}',
											[r.message.synced || 0, r.message.skipped || 0, r.message.errors || 0]),
										indicator: (r.message.errors || 0) > 0 ? 'orange' : 'green'
									});
								}
							}
						});
					}
				});
				dialog.show();
			}, __('Daten'));

			// Sync Customers
			frm.add_custom_button(__('Kunden synchronisieren'), function() {
				let dialog = new frappe.ui.Dialog({
					title: __('Kunden von Shopware importieren'),
					fields: [
						{
							fieldname: 'limit',
							fieldtype: 'Int',
							label: __('Limit'),
							default: 100
						}
					],
					primary_action_label: __('Importieren'),
					primary_action: function(values) {
						dialog.hide();
						frappe.call({
							method: 'ecommerce_integrations.shopware6.customer.sync_customers_from_shopware',
							args: values,
							freeze: true,
							freeze_message: __('Importiere Kunden...'),
							callback: function(r) {
								if (r.message) {
									frappe.msgprint({
										title: __('Kunden-Import'),
										message: __('Importiert: {0}, Übersprungen: {1}, Fehler: {2}',
											[r.message.synced || 0, r.message.skipped || 0, r.message.errors || 0]),
										indicator: (r.message.errors || 0) > 0 ? 'orange' : 'green'
									});
								}
							}
						});
					}
				});
				dialog.show();
			}, __('Daten'));
		}
	},

	load_queue_status: function(frm) {
		frappe.call({
			method: 'ecommerce_integrations.shopware6.bulk_sync.get_queue_status',
			callback: function(r) {
				if (r.message) {
					let status = r.message;
					let html = `
						<div class="bulk-sync-status" style="padding: 15px; background: var(--bg-light-gray); border-radius: 4px; margin-top: 10px;">
							<h6 style="margin-bottom: 10px; font-weight: bold;">Bulk Sync Queue Status</h6>
							<p style="margin: 5px 0;"><strong>Queue:</strong> ${status.queue_size || 0}
								<span class="text-muted">(Produkte: ${status.product_queue_size || 0}, Properties: ${status.properties_queue_size || 0})</span>
							</p>
							<p style="margin: 5px 0;"><strong>Bulk Mode:</strong>
								<span class="indicator-pill ${status.bulk_mode_active ? 'green' : 'gray'}">
									${status.bulk_mode_active ? 'Aktiv' : 'Inaktiv'}
								</span>
							</p>
							<p style="margin: 5px 0;"><strong>Verarbeitung:</strong>
								<span class="indicator-pill ${status.processing ? 'orange' : 'gray'}">
									${status.processing ? 'Ja' : 'Nein'}
								</span>
							</p>
						</div>
					`;

					if (status.product_queue && status.product_queue.length > 0) {
						html += `
							<div style="margin-top: 10px; font-size: 12px;">
								<strong>In Queue (erste 10):</strong>
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
