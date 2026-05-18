// Plain-language traffic-light health banner rendered at the top of
// Shopware Setting / Medusa Setting. Loaded via `doctype_js` in hooks.py
// for both forms; the parent setting JS calls
// window.ecom_health_banner.render(frm, '<Backend>').
//
// Reads its state from the backend-specific
// `<backend>.api.setup.get_health` endpoint and renders one of four states:
//
//   green  — connection healthy
//   yellow — needs attention (not tested recently / errors / no recent syncs)
//   red    — broken (misconfigured webhook / failed test connection)
//   grey   — integration disabled
//
// Provides two side buttons: "Details" (opens recent Integration Log rows in
// a Dialog) and "Safety Mode" (delegates to window.ecom_safety_mode).

(function () {

	const BACKEND_MAP = {
		Shopware: {
			module: 'ecommerce_integrations.shopware6.api.setup',
		},
		Medusa: {
			module: 'ecommerce_integrations.medusa.api.setup',
		},
	};

	// Lucide icon + Frappe color tokens per state. ``ecom_icon`` is
	// loaded ahead of this script via doctype_js; the soft fallback
	// keeps the banner usable on a stray page where the helper is
	// absent.
	function _state_icon(name, color) {
		if (!window.ecom_icon) return '';
		return window.ecom_icon(name, 'sm', color);
	}
	const STATE_STYLES = {
		green:  { icon: () => _state_icon('circle-check',  '#28a745'), bg: '#e8f6ee', border: '#28a745' },
		yellow: { icon: () => _state_icon('circle-alert',  '#f0ad4e'), bg: '#fff8e1', border: '#f0ad4e' },
		red:    { icon: () => _state_icon('circle-x',      '#d9534f'), bg: '#fdecea', border: '#d9534f' },
		grey:   { icon: () => _state_icon('circle',        '#adb5bd'), bg: '#f1f3f5', border: '#adb5bd' },
	};

	function fmt_relative(value) {
		if (!value) return '—';
		try {
			return frappe.datetime.comment_when(value);
		} catch (e) {
			return frappe.datetime.str_to_user(value);
		}
	}

	function build_banner_html(state, data) {
		const style = STATE_STYLES[state] || STATE_STYLES.grey;
		const last_test_line = data.last_test_at
			? __('Letzter Verbindungstest: {0}', [fmt_relative(data.last_test_at)])
			: __('Verbindung noch nicht getestet');
		const stats_line = __('{0} Fehler in 24h · {1} erfolgreiche Syncs',
			[data.error_24h || 0, data.success_24h || 0]);
		const detail = data.detail
			? `<div class="text-muted small" style="margin-top:2px">${frappe.utils.escape_html(data.detail)}</div>`
			: '';
		return `
			<div class="ecom-health-banner"
			     style="border-left:4px solid ${style.border};background:${style.bg};
			            padding:10px 14px;margin:0 0 12px 0;border-radius:4px;
			            display:flex;justify-content:space-between;align-items:center;gap:12px">
				<div style="flex:1;min-width:0">
					<div style="font-weight:600;font-size:13px">
						<span style="margin-right:6px;vertical-align:middle">${style.icon()}</span>
						${frappe.utils.escape_html(data.message || '')}
					</div>
					${detail}
					<div class="text-muted small" style="margin-top:4px">
						${last_test_line} · ${stats_line}
					</div>
				</div>
				<div class="ecom-health-banner-actions" style="flex-shrink:0;display:flex;gap:6px">
					<button type="button" class="btn btn-xs btn-default ehb-details-btn">
						${__('Details')}
					</button>
					<button type="button" class="btn btn-xs btn-default ehb-refresh-btn">
						${__('Aktualisieren')}
					</button>
					<button type="button" class="btn btn-xs btn-warning ehb-safety-btn">
						${__('Safety Mode')}
					</button>
				</div>
			</div>
		`;
	}

	function show_details_dialog(backend, module) {
		const d = new frappe.ui.Dialog({
			title: __('Details — Letzte Ereignisse ({0})', [backend]),
			size: 'large',
			fields: [{ fieldname: 'log_html', fieldtype: 'HTML' }],
		});
		d.show();
		const $wrap = d.fields_dict.log_html.$wrapper;
		$wrap.html(`<div class="text-muted small">${__('Lädt …')}</div>`);
		frappe.call({
			method: module + '.recent_logs',
			args: { limit: 30 },
			callback: function (r) {
				const rows = r.message || [];
				if (!rows.length) {
					$wrap.html(`<div class="text-muted small">${__('Keine Einträge gefunden.')}</div>`);
					return;
				}
				let html = '<table class="table table-sm" style="margin-bottom:0">';
				html += `<thead><tr>
					<th style="width:140px">${__('Zeitpunkt')}</th>
					<th style="width:90px">${__('Status')}</th>
					<th>${__('Methode')}</th>
					<th>${__('Nachricht')}</th>
				</tr></thead><tbody>`;
				rows.forEach(function (row) {
					const status_class = row.status === 'Error'
						? 'red'
						: (row.status === 'Success' ? 'green' : 'gray');
					const msg = (row.message || '').toString().slice(0, 200);
					html += '<tr>'
						+ `<td class="small text-muted">${frappe.utils.escape_html(fmt_relative(row.creation))}</td>`
						+ `<td><span class="indicator-pill ${status_class}">${frappe.utils.escape_html(row.status || '—')}</span></td>`
						+ `<td class="small">${frappe.utils.escape_html(row.method || '—')}</td>`
						+ `<td class="small">${frappe.utils.escape_html(msg)}</td>`
						+ '</tr>';
				});
				html += '</tbody></table>';
				$wrap.html(html);
			},
		});
	}

	function render(frm, backend) {
		if (!frm || !frm.layout || !frm.layout.wrapper) return;
		const cfg = BACKEND_MAP[backend];
		if (!cfg) return;

		const $form = $(frm.layout.wrapper);
		$form.find('.ecom-health-banner-host').remove();
		const $host = $('<div class="ecom-health-banner-host"></div>');
		$form.find('.form-layout, .layout-main, .form-page').first().prepend($host);
		if (!$host.parent().length) {
			$form.prepend($host);
		}

		function paint() {
			$host.html(`<div class="text-muted small" style="margin-bottom:10px">${__('Status wird geladen …')}</div>`);
			frappe.call({
				method: cfg.module + '.get_health',
				callback: function (r) {
					const data = r.message || { state: 'grey', message: __('Status nicht verfügbar') };
					$host.html(build_banner_html(data.state, data));
					$host.find('.ehb-refresh-btn').on('click', paint);
					$host.find('.ehb-details-btn').on('click', function () {
						show_details_dialog(backend, cfg.module);
					});
					$host.find('.ehb-safety-btn').on('click', function () {
						if (window.ecom_safety_mode && window.ecom_safety_mode.confirm_and_apply) {
							window.ecom_safety_mode.confirm_and_apply(frm, backend, paint);
						} else {
							frappe.msgprint(__('Safety Mode ist nicht verfügbar.'));
						}
					});
				},
				error: function () {
					$host.html(
						`<div class="text-muted small" style="margin-bottom:10px">${__('Status nicht verfügbar.')}</div>`
					);
				},
			});
		}

		paint();
	}

	window.ecom_health_banner = { render: render };
})();
