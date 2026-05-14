frappe.ui.form.on('Ecommerce Smart Collection', {
	refresh(frm) {
		if (frm.is_new()) return;

		// Surface error targets on the form dashboard so an operator
		// landing on a problematic collection sees the red flag without
		// scrolling into the Targets table.
		const error_targets = (frm.doc.targets || []).filter(
			(t) => t.sync_status === 'error',
		);
		if (error_targets.length) {
			frm.dashboard.add_indicator(
				__('{0} target(s) in error', [error_targets.length]),
				'red',
			);
		}
		const running_targets = (frm.doc.targets || []).filter(
			(t) => t.sync_status === 'running',
		);
		if (running_targets.length) {
			frm.dashboard.add_indicator(
				__('{0} target(s) running', [running_targets.length]),
				'blue',
			);
		}

		// Resolver state intro — operator-visible "what we know about
		// this collection" line. Scheduling cadence is rough — the
		// hourly_long entry runs once an hour at the bench's
		// configured offset, surfacing the exact next-run time would
		// need a server round-trip we don't pay for here.
		const resolved = frm.doc.last_resolved_count;
		const resolved_at = frm.doc.last_resolved_at;
		if (resolved !== undefined && resolved !== null) {
			const when = resolved_at
				? frappe.datetime.comment_when(resolved_at)
				: __('not yet resolved');
			frm.set_intro(
				__('Last resolver run: {0} item(s), {1}. Scheduled sync: hourly.', [
					resolved,
					when,
				]),
				'blue',
			);
		}

		frm.add_custom_button(__('Preview Matches'), function () {
			frappe.call({
				method: 'ecommerce_integrations.smart_collections.api.preview',
				args: { collection: frm.doc.name },
				freeze: true,
				freeze_message: __('Resolving rules…'),
				callback(r) {
					if (!r.message) return;
					_show_resolver_dialog(frm, r.message);
				},
			});
		}, __('Smart Collections'));

		frm.add_custom_button(__('Preview Sync'), function () {
			frappe.call({
				method: 'ecommerce_integrations.smart_collections.api.preview_collection',
				args: { collection: frm.doc.name },
				freeze: true,
				freeze_message: __('Computing dry-run…'),
				callback(r) {
					if (!r.message) return;
					_show_sync_preview_dialog(frm, r.message);
				},
			});
		}, __('Smart Collections')).addClass('btn-primary');
	},
});


function _show_resolver_dialog(frm, preview) {
	const sample_html = (preview.sample_items || [])
		.map(
			(it) =>
				`<tr>
					<td>${frappe.utils.escape_html(it.name)}</td>
					<td>${frappe.utils.escape_html(it.item_name || '')}</td>
					<td class="text-muted small">${frappe.utils.escape_html(it.item_group || '')}</td>
				</tr>`,
		)
		.join('');
	const breakdown_html = (preview.breakdown_by_rule || [])
		.map(
			(r, i) =>
				`<tr>
					<td>${i + 1}</td>
					<td>${frappe.utils.escape_html(r.rule_type)}</td>
					<td class="small">${frappe.utils.escape_html(r.field_key || '')} ${frappe.utils.escape_html(r.operator)} ${frappe.utils.escape_html(r.value || '')}</td>
					<td class="text-right">${r.matching_count}</td>
				</tr>`,
		)
		.join('');
	const html = `
		<div>
			<p><strong>${preview.count}</strong> ${__('items match this collection')}.</p>
			<h5>${__('Per-rule breakdown')}</h5>
			<table class="table table-sm">
				<thead><tr><th>#</th><th>${__('Type')}</th><th>${__('Condition')}</th><th class="text-right">${__('Matches')}</th></tr></thead>
				<tbody>${breakdown_html}</tbody>
			</table>
			<h5 style="margin-top:1em">${__('Sample (50 max)')}</h5>
			<table class="table table-sm">
				<thead><tr><th>${__('Item Code')}</th><th>${__('Name')}</th><th>${__('Item Group')}</th></tr></thead>
				<tbody>${sample_html || `<tr><td colspan="3" class="text-muted">${__('No items match')}</td></tr>`}</tbody>
			</table>
		</div>`;
	const d = new frappe.ui.Dialog({
		title: __('Preview Matches: {0}', [frm.doc.title]),
		size: 'large',
		fields: [{ fieldname: 'preview_html', fieldtype: 'HTML' }],
	});
	d.fields_dict.preview_html.$wrapper.html(html);
	d.show();
}


function _show_sync_preview_dialog(frm, plan) {
	const d = new frappe.ui.Dialog({
		title: __('Preview Sync: {0}', [plan.title || frm.doc.name]),
		size: 'extra-large',
		fields: [{ fieldname: 'preview_html', fieldtype: 'HTML' }],
		primary_action_label: __('Run Live Sync'),
		primary_action() {
			frappe.confirm(
				__('Run live sync now? This will apply the changes shown above.'),
				function () {
					d.hide();
					frappe.call({
						method: 'ecommerce_integrations.smart_collections.tasks.sync_collection_now',
						args: { collection: frm.doc.name },
						freeze: true,
						freeze_message: __('Syncing to backends…'),
						callback(r) {
							if (r.message?.skipped) {
								frappe.show_alert({
									message: __('Skipped: {0}', [r.message.reason]),
									indicator: 'orange',
								});
							} else {
								frappe.show_alert({
									message: __('Sync complete'),
									indicator: 'green',
								});
								frm.reload_doc();
							}
						},
					});
				},
			);
		},
	});
	d.fields_dict.preview_html.$wrapper.html(_render_sync_preview(plan));
	// Wire Adopt buttons rendered into the dialog HTML.
	d.fields_dict.preview_html.$wrapper.on('click', '.adopt-btn', function () {
		const $btn = $(this);
		const target_name = $btn.data('target');
		const external_id = $btn.data('ext');
		const link_only = $btn.data('link-only') ? 1 : 0;
		frappe.call({
			method: 'ecommerce_integrations.smart_collections.api.adopt_match',
			args: { target_name, external_id, link_only },
			freeze: true,
			freeze_message: __('Adopting…'),
			callback() {
				frappe.show_alert({
					message: __('Adopted backend category'),
					indicator: 'green',
				});
				d.hide();
				frm.reload_doc();
			},
		});
	});
	d.show();
}


function _render_sync_preview(plan) {
	if (plan.skipped) {
		return `<div class="alert alert-warning">${__('Skipped: {0}', [plan.skip_reason || ''])}</div>`;
	}
	const head = `
		<div>
			<p>${__('Resolved {0} item(s).', [plan.resolved_count])}
			${plan.targets.length === 0 ? __('No enabled targets.') : ''}</p>
		</div>`;
	const sections = (plan.targets || []).map(_render_target_section).join('');
	return head + sections;
}


function _render_target_section(t) {
	const action_color =
		t.action === 'create' ? 'green' : t.action === 'update' ? 'orange' : 'gray';
	const link_only_badge = t.link_only
		? `<span class="indicator-pill blue">${__('Link only')}</span>`
		: '';
	const removed_label = t.to_remove_skipped
		? `${t.to_remove.length} ${__('(SKIPPED — link_only)')}`
		: `${t.to_remove.length}`;
	const matches = (t.match_suggestions || [])
		.map((m) => {
			const sc_badge = m.has_target_sales_channel
				? `<span class="indicator-pill green">${__('on target channel')}</span>`
				: '';
			return `
				<tr>
					<td>${frappe.utils.escape_html(m.name)}</td>
					<td class="small text-muted">${frappe.utils.escape_html(m.path)}</td>
					<td class="text-right">${m.linked_product_count}</td>
					<td>${sc_badge}</td>
					<td>
						<button class="btn btn-xs btn-default adopt-btn"
							data-target="${frappe.utils.escape_html(t.target_name)}"
							data-ext="${frappe.utils.escape_html(m.external_id)}">
							${__('Adopt')}
						</button>
						<button class="btn btn-xs btn-default adopt-btn"
							data-target="${frappe.utils.escape_html(t.target_name)}"
							data-ext="${frappe.utils.escape_html(m.external_id)}"
							data-link-only="1">
							${__('Adopt + Link Only')}
						</button>
					</td>
				</tr>`;
		})
		.join('');
	const matches_html = matches
		? `<h6>${__('Adopt existing backend category')}</h6>
			<table class="table table-sm">
				<thead><tr>
					<th>${__('Name')}</th><th>${__('Path')}</th>
					<th class="text-right">${__('Products')}</th>
					<th></th><th></th>
				</tr></thead>
				<tbody>${matches}</tbody>
			</table>`
		: '';
	const notes = (t.notes || [])
		.map((n) => `<div class="text-muted small">${frappe.utils.escape_html(n)}</div>`)
		.join('');
	const unresolved = (t.unresolved_items || []).slice(0, 100);
	const more = (t.unresolved_items || []).length - unresolved.length;
	const unresolved_html = unresolved.length
		? `<details>
			<summary>${__('Unresolved items')}: ${t.unresolved_items.length}</summary>
			<div class="small">${unresolved.map(frappe.utils.escape_html).join(', ')}${more > 0 ? ` ${__('and {0} more', [more])}` : ''}</div>
		</details>`
		: '';
	const drift_html = (t.drift_local_only && t.drift_local_only.length) || t.drift_backend_only_count
		? `<div class="text-warning small">
			${__('Backend drift')}:
			${t.drift_local_only ? t.drift_local_only.length : 0} ${__('expected but missing in backend')},
			${t.drift_backend_only_count} ${__('present in backend but not tracked locally')}
		</div>`
		: '';
	return `
		<div class="border" style="padding:0.75em;margin-bottom:0.75em;border-radius:4px">
			<h5>
				${frappe.utils.escape_html(t.backend)} / ${frappe.utils.escape_html(t.sales_channel || '—')}
				<span class="indicator-pill ${action_color}" style="margin-left:0.5em">${t.action.toUpperCase()}</span>
				${link_only_badge}
			</h5>
			<div class="small text-muted">
				${__('Name')}: ${frappe.utils.escape_html(t.proposed_name)}
				${t.live_exists ? `→ ${__('live')}: ${frappe.utils.escape_html(t.live_name || '')}` : ''}
				| ${__('external_id')}: ${frappe.utils.escape_html(t.external_id || '—')}
			</div>
			<div style="margin-top:0.5em">
				<span class="indicator-pill green">${__('Add')}: ${t.to_add.length}</span>
				<span class="indicator-pill red">${__('Remove')}: ${removed_label}</span>
				<span class="indicator-pill gray">${__('Keep')}: ${t.to_keep.length}</span>
			</div>
			${drift_html}
			${notes}
			${unresolved_html}
			${matches_html}
		</div>`;
}
