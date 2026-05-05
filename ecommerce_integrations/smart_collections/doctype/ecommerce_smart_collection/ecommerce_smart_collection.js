frappe.ui.form.on('Ecommerce Smart Collection', {
	refresh(frm) {
		if (frm.is_new()) return;

		frm.add_custom_button(__('Preview Matches'), function() {
			frappe.call({
				method: 'ecommerce_integrations.smart_collections.api.preview',
				args: { collection: frm.doc.name },
				freeze: true,
				freeze_message: __('Resolving rules…'),
				callback(r) {
					if (!r.message) return;
					_show_preview_dialog(frm, r.message);
				}
			});
		}, __('Smart Collections'));

		frm.add_custom_button(__('Sync Now'), function() {
			frappe.confirm(
				__('Push this collection to all enabled targets now?'),
				function() {
					frappe.call({
						method: 'ecommerce_integrations.smart_collections.tasks.sync_collection_now',
						args: { collection: frm.doc.name },
						freeze: true,
						freeze_message: __('Syncing to backends…'),
						callback(r) {
							if (r.message?.skipped) {
								frappe.show_alert({
									message: __('Skipped: {0}', [r.message.reason]),
									indicator: 'orange'
								});
							} else {
								frappe.show_alert({
									message: __('Sync complete'),
									indicator: 'green'
								});
								frm.reload_doc();
							}
						}
					});
				}
			);
		}, __('Smart Collections'));
	}
});


function _show_preview_dialog(frm, preview) {
	const sample_html = (preview.sample_items || []).map(function(it) {
		return `<tr>
			<td>${frappe.utils.escape_html(it.name)}</td>
			<td>${frappe.utils.escape_html(it.item_name || '')}</td>
			<td class="text-muted small">${frappe.utils.escape_html(it.item_group || '')}</td>
		</tr>`;
	}).join('');

	const breakdown_html = (preview.breakdown_by_rule || []).map(function(r, i) {
		return `<tr>
			<td>${i + 1}</td>
			<td>${frappe.utils.escape_html(r.rule_type)}</td>
			<td class="small">${frappe.utils.escape_html(r.field_key || '')} ${frappe.utils.escape_html(r.operator)} ${frappe.utils.escape_html(r.value || '')}</td>
			<td class="text-right">${r.matching_count}</td>
		</tr>`;
	}).join('');

	const html = `
		<div>
			<p><strong>${preview.count}</strong> items match this collection${preview.count > 50 ? ' (showing first 50)' : ''}.</p>
			<h5>Per-rule breakdown</h5>
			<table class="table table-sm">
				<thead><tr><th>#</th><th>Type</th><th>Condition</th><th class="text-right">Matches</th></tr></thead>
				<tbody>${breakdown_html}</tbody>
			</table>
			<h5 style="margin-top:1em">Sample (50 max)</h5>
			<table class="table table-sm">
				<thead><tr><th>Item Code</th><th>Name</th><th>Item Group</th></tr></thead>
				<tbody>${sample_html || '<tr><td colspan="3" class="text-muted">No items match</td></tr>'}</tbody>
			</table>
		</div>
	`;

	const d = new frappe.ui.Dialog({
		title: __('Preview Matches: {0}', [frm.doc.title]),
		size: 'large',
		fields: [{ fieldname: 'preview_html', fieldtype: 'HTML' }]
	});
	d.fields_dict.preview_html.$wrapper.html(html);
	d.show();
}
