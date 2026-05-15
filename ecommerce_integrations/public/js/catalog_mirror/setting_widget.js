// Catalog Mirror widget rendered into Medusa Setting / Shopware Setting.
// Loaded via `doctype_js` in hooks.py for both forms; the parent setting JS
// calls window.catalog_mirror_widget.render(frm, '<Backend>').
//
// Mirrors the smart_collections/setting_widget.js pattern — same layout
// language, different data model. Each row is one Catalog Mirror (=
// one ERPNext Item Group tree → one backend category tree).

(function () {

	function status_badge(row) {
		const map = { ok: 'green', error: 'red', running: 'blue', pending: 'orange' };
		const colour = map[row.sync_status] || 'gray';
		const label = row.sync_status || 'pending';
		return `<span class="indicator-pill ${colour}">${frappe.utils.escape_html(label)}</span>`;
	}

	function active_badge(row) {
		if (row.is_active) return `<span class="text-success">${__('active')}</span>`;
		return `<span class="text-muted">${__('inactive')}</span>`;
	}

	function adopted_badge(row) {
		if (row.external_root_id) {
			return `<span class="indicator-pill green" title="${frappe.utils.escape_html(row.external_root_id)}">${__('adopted')}</span>`;
		}
		return `<span class="indicator-pill orange">${__('new')}</span>`;
	}

	function build_table_html(rows, backend) {
		let html = '<div class="catalog-mirror-summary">';
		let cols = '<th>' + __('Title') + '</th>'
			+ '<th style="width:110px">' + __('Active') + '</th>'
			+ '<th style="width:140px">' + __('Root Item Group') + '</th>';
		if (backend === 'Shopware') {
			cols += '<th style="width:160px">' + __('Sales Channel') + '</th>';
		}
		cols += '<th style="width:90px">' + __('Root') + '</th>'
			+ '<th style="width:90px">' + __('Status') + '</th>'
			+ '<th style="width:160px">' + __('Last Synced') + '</th>'
			+ '<th style="width:60px">' + __('Open') + '</th>';

		html += `<table class="table table-sm" style="margin-bottom:0">`
			+ `<thead><tr>${cols}</tr></thead><tbody>`;

		rows.forEach(function (row) {
			const link = `/app/ecommerce-catalog-mirror/${encodeURIComponent(row.name)}`;
			const sales_channel = row.target_sales_channel || row.target_sales_channel_data || '—';
			html += '<tr>';
			html += `<td>${frappe.utils.escape_html(row.title || row.name)}</td>`;
			html += `<td>${active_badge(row)}</td>`;
			html += `<td>${frappe.utils.escape_html(row.root_item_group || '—')}</td>`;
			if (backend === 'Shopware') {
				html += `<td class="small text-muted">${frappe.utils.escape_html(sales_channel)}</td>`;
			}
			html += `<td>${adopted_badge(row)}</td>`;
			html += `<td>${status_badge(row)}</td>`;
			html += `<td class="text-muted small">${row.last_synced_at ? frappe.datetime.str_to_user(row.last_synced_at) : '—'}</td>`;
			html += `<td><a href="${link}">→</a></td>`;
			html += '</tr>';
		});
		html += '</tbody></table>';
		html += '</div>';
		return html;
	}

	function open_create_dialog(backend, channels, on_created) {
		const fields = [
			{
				fieldname: 'title', fieldtype: 'Data', label: __('Title'), reqd: 1,
				description: __('Operator-facing label. Example: "yourshop.com / Produkte".'),
			},
			{
				fieldname: 'root_item_group', fieldtype: 'Link', label: __('Root Item Group'),
				options: 'Item Group', reqd: 1,
				description: __('The ERPNext Item Group where the mirror tree starts. The full subtree below this IG will mirror to the backend.'),
			},
		];

		if (backend === 'Shopware') {
			const channel_options = channels
				.filter((c) => c.active !== 0)
				.map((c) => c.id);
			if (!channel_options.length) {
				frappe.msgprint(__('No active Sales Channels configured for {0}. Configure one first.', [backend]));
				return;
			}
			fields.push({
				fieldname: 'target_sales_channel', fieldtype: 'Select',
				label: __('Sales Channel'),
				options: channel_options.join('\n'), reqd: 1,
				description: __('The backend sales channel this mirror feeds.'),
			});
		} else {
			fields.push({
				fieldname: 'target_sales_channel_data', fieldtype: 'Data',
				label: __('Sales Channel (optional)'),
				description: __('Medusa: not used — categories are not channel-scoped.'),
			});
		}

		fields.push({
			fieldname: 'orphan_policy', fieldtype: 'Select',
			label: __('Orphan Policy'),
			options: 'keep\nreport\ndelete', default: 'keep',
			description: __('What to do with backend categories that have no matching ERPNext Item Group. Default "keep" is safest — never auto-deletes.'),
		});
		fields.push({
			fieldname: 'is_active', fieldtype: 'Check', label: __('Active'), default: 0,
			description: __('Leave off until you have run a Preview Sync and confirmed the tree diff. Then activate from the form.'),
		});

		const d = new frappe.ui.Dialog({
			title: __('New Catalog Mirror — {0}', [backend]),
			fields: fields,
			primary_action_label: __('Create & Edit'),
			primary_action: function (values) {
				const doc = {
					doctype: 'Ecommerce Catalog Mirror',
					title: values.title,
					backend: backend,
					root_item_group: values.root_item_group,
					orphan_policy: values.orphan_policy || 'keep',
					is_active: values.is_active ? 1 : 0,
				};
				if (backend === 'Shopware') {
					doc.target_sales_channel = values.target_sales_channel;
				} else if (values.target_sales_channel_data) {
					doc.target_sales_channel_data = values.target_sales_channel_data;
				}
				frappe.call({
					method: 'frappe.client.insert',
					args: { doc: doc },
					callback: function (r) {
						d.hide();
						if (r.message) {
							frappe.show_alert({
								message: __('Created. Run a Preview Sync before activating.'),
								indicator: 'green',
							});
							if (on_created) on_created(r.message.name);
						}
					},
				});
			},
		});
		d.show();
	}

	function render(frm, backend) {
		const wrap = frm.fields_dict.catalog_mirror_html;
		if (!wrap || !wrap.$wrapper) return;

		const channels = (frm.doc.sales_channels || []).map(function (c) {
			return {
				label: c.sales_channel_name || c.sales_channel_id,
				id: c.sales_channel_id || c.sales_channel_name,
				active: c.active !== 0,
			};
		}).filter((c) => c.id);

		const list_url = `/app/ecommerce-catalog-mirror/view/list?backend=${encodeURIComponent(backend)}`;
		const header = `
			<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
				<div>
					<button class="btn btn-primary btn-sm cm-add-btn" type="button">
						<i class="fa fa-plus"></i> ${__('Add Mirror')}
					</button>
					<a class="btn btn-default btn-sm" href="${list_url}" target="_blank">${__('Browse all')}</a>
				</div>
				<button class="btn btn-default btn-xs cm-refresh-btn" type="button">${__('Refresh')}</button>
			</div>
		`;

		function reload() {
			wrap.$wrapper.html(header + '<div class="cm-body text-muted small">' + __('Loading…') + '</div>');
			wrap.$wrapper.find('.cm-add-btn').on('click', function () {
				open_create_dialog(backend, channels, function (name) {
					reload();
					if (name) frappe.set_route('Form', 'Ecommerce Catalog Mirror', name);
				});
			});
			wrap.$wrapper.find('.cm-refresh-btn').on('click', reload);

			frappe.call({
				method: 'ecommerce_integrations.catalog_mirror.api.list_for_backend',
				args: { backend: backend },
				callback: function (r) {
					const rows = r.message || [];
					const $body = wrap.$wrapper.find('.cm-body');
					if (!rows.length) {
						$body.html(
							`<div class="text-muted small">${__('No Catalog Mirrors target {0} yet — use the Add Mirror button above. One Mirror per storefront × root Item Group.', [backend])}</div>`
						);
						return;
					}
					$body.html(build_table_html(rows, backend));
				},
				error: function () {
					wrap.$wrapper.find('.cm-body').html(
						`<div class="text-muted small">${__('Catalog Mirror module not available — run bench migrate.')}</div>`
					);
				},
			});
		}

		reload();
	}

	window.catalog_mirror_widget = { render: render };
})();
