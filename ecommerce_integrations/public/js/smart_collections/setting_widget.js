// Smart Collections widget rendered into Medusa Setting / Shopware Setting.
// Loaded via `doctype_js` in hooks.py for both forms; the parent setting JS
// just calls window.smart_collections_widget.render(frm, '<Backend>').

(function() {

	function status_badge(row) {
		const map = { ok: 'green', error: 'red', pending: 'orange' };
		const colour = map[row.sync_status] || 'gray';
		return `<span class="indicator-pill ${colour}">${row.sync_status || 'pending'}</span>`;
	}

	function active_badge(row) {
		if (!row.is_active) return `<span class="text-muted">${__('inactive')}</span>`;
		if (!row.enabled) return `<span class="text-warning">${__('disabled')}</span>`;
		return `<span class="text-success">${__('on')}</span>`;
	}

	function build_table_html(groups, opts) {
		const show_visibility = !!opts.show_visibility;
		let html = '<div class="smart-collections-summary">';
		Object.keys(groups).sort().forEach(function(channel) {
			html += `<h5 style="margin-top:1em">${frappe.utils.escape_html(channel)}</h5>`;
			let cols = `<th>${__('Collection')}</th>`
				+ `<th style="width:90px">${__('State')}</th>`
				+ `<th style="width:90px">${__('Items')}</th>`;
			if (show_visibility) cols += `<th style="width:90px">${__('Visibility')}</th>`;
			cols += `<th style="width:120px">${__('Status')}</th>`
				+ `<th style="width:160px">${__('Last Synced')}</th>`
				+ `<th style="width:60px">${__('Open')}</th>`;
			html += `<table class="table table-sm" style="margin-bottom:0">`
				+ `<thead><tr>${cols}</tr></thead><tbody>`;
			groups[channel].forEach(function(row) {
				const link = `/app/ecommerce-smart-collection/${encodeURIComponent(row.collection)}`;
				html += '<tr>';
				html += `<td>${frappe.utils.escape_html(row.title)}</td>`;
				html += `<td>${active_badge(row)}</td>`;
				html += `<td>${row.last_resolved_count || 0}</td>`;
				if (show_visibility) html += `<td class="small">${frappe.utils.escape_html(row.visibility || '—')}</td>`;
				html += `<td>${status_badge(row)}</td>`;
				html += `<td class="text-muted small">${row.last_synced_at ? frappe.datetime.str_to_user(row.last_synced_at) : '—'}</td>`;
				html += `<td><a href="${link}">→</a></td>`;
				html += '</tr>';
			});
			html += '</tbody></table>';
		});
		html += '</div>';
		return html;
	}

	function open_create_dialog(backend, channels, on_created) {
		const channel_options = channels
			.filter(c => c.active !== 0)
			.map(c => c.id);
		if (!channel_options.length) {
			frappe.msgprint(__('No active Sales Channels configured for {0}.', [backend]));
			return;
		}
		const fields = [
			{ fieldname: 'title', fieldtype: 'Data', label: __('Title'), reqd: 1 },
			{
				fieldname: 'sales_channel', fieldtype: 'Select', label: __('Sales Channel'),
				options: channel_options.join('\n'), reqd: 1,
			},
		];
		if (backend === 'Shopware') {
			fields.push({
				fieldname: 'visibility', fieldtype: 'Select', label: __('Visibility'),
				options: 'All (30)\nLinked Only (20)\nSearch Only (10)\nHidden (0)',
				default: 'All (30)',
			});
		}
		fields.push({
			fieldname: 'is_active', fieldtype: 'Check', label: __('Active'), default: 0,
			description: __('Leave off until rules are configured, then activate from the form.'),
		});

		const d = new frappe.ui.Dialog({
			title: __('New Smart Collection — {0}', [backend]),
			fields: fields,
			primary_action_label: __('Create & Edit'),
			primary_action: function(values) {
				frappe.call({
					method: 'frappe.client.insert',
					args: {
						doc: {
							doctype: 'Ecommerce Smart Collection',
							title: values.title,
							is_active: values.is_active ? 1 : 0,
							rule_combinator: 'AND',
							targets: [{
								backend: backend,
								sales_channel: values.sales_channel,
								enabled: 1,
								visibility: values.visibility || 'All (30)',
							}],
						},
					},
					callback: function(r) {
						d.hide();
						if (r.message) {
							frappe.show_alert({
								message: __('Created. Add rules to start matching items.'),
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
		const wrap = frm.fields_dict.smart_collections_html;
		if (!wrap || !wrap.$wrapper) return;

		const opts = { show_visibility: backend === 'Shopware' };
		const channels = (frm.doc.sales_channels || []).map(function(c) {
			return {
				label: c.sales_channel_name || c.sales_channel_id,
				id: c.sales_channel_id || c.sales_channel_name,
				active: c.active !== 0,
			};
		}).filter(c => c.id);

		const list_url = `/app/ecommerce-smart-collection/view/list?targets.backend=${encodeURIComponent(backend)}`;
		const header = `
			<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
				<div>
					<button class="btn btn-primary btn-sm sc-add-btn" type="button">
						<i class="fa fa-plus"></i> ${__('Add Collection')}
					</button>
					<a class="btn btn-default btn-sm" href="${list_url}" target="_blank">${__('Browse all')}</a>
				</div>
				<button class="btn btn-default btn-xs sc-refresh-btn" type="button">${__('Refresh')}</button>
			</div>
		`;

		function reload() {
			wrap.$wrapper.html(header + `<div class="sc-body text-muted small">${__('Loading…')}</div>`);
			wrap.$wrapper.find('.sc-add-btn').on('click', function() {
				open_create_dialog(backend, channels, function(name) {
					reload();
					if (name) frappe.set_route('Form', 'Ecommerce Smart Collection', name);
				});
			});
			wrap.$wrapper.find('.sc-refresh-btn').on('click', reload);

			frappe.call({
				method: 'ecommerce_integrations.smart_collections.api.list_for_backend',
				args: { backend },
				callback: function(r) {
					const rows = r.message || [];
					const $body = wrap.$wrapper.find('.sc-body');
					if (!rows.length) {
						$body.html(
							`<div class="text-muted small">${__('No Smart Collections target {0} yet — use the Add Collection button above.', [backend])}</div>`
						);
						return;
					}
					const groups = {};
					rows.forEach(function(row) {
						(groups[row.sales_channel] = groups[row.sales_channel] || []).push(row);
					});
					$body.html(build_table_html(groups, opts));
				},
			});
		}

		reload();
	}

	window.smart_collections_widget = { render: render };
})();
