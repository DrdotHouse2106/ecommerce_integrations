// Inline Webhook URL widget rendered into Shopware Setting / Medusa Setting.
// Loaded via `doctype_js` in hooks.py for both forms; the parent setting JS
// calls window.ecom_webhook_widget.render(frm, '<Backend>').
//
// Renders into the existing `webhook_url_info` HTML field if present;
// otherwise inserts a host element after the section_break_webhook section.

(function () {

	const BACKEND_MAP = {
		Shopware: {
			module: 'ecommerce_integrations.shopware6.api.setup',
			host_field: 'webhook_url_info',
			docs_url: 'https://developer.shopware.com/docs/guides/hosting/installation-updates/cluster-setup.html',
			events_hint: __('order.placed, customer.written, product_stock.written'),
			setup_hint: __('In Shopware: Einstellungen → System → Webhooks → "Webhook hinzufügen".'),
		},
		Medusa: {
			module: 'ecommerce_integrations.medusa.api.setup',
			host_field: 'webhook_url_info',
			docs_url: 'https://docs.medusajs.com/',
			events_hint: __('order.placed, customer.created, inventory.updated'),
			setup_hint: __('In Medusa Admin: aktiviere das Subscriber-Plugin und trage URL + Secret ein.'),
		},
	};

	function fmt_relative(value) {
		if (!value) return null;
		try {
			return frappe.datetime.comment_when(value);
		} catch (e) {
			return frappe.datetime.str_to_user(value);
		}
	}

	function build_html(backend, data, secret_visible) {
		const cfg = BACKEND_MAP[backend];
		const url = data.url || '';
		const masked = '••••••••••••••••••••••••••••••••';
		const secret_display = data.secret_set
			? (secret_visible ? __('(value only visible via form field)') : masked)
			: `<span class="text-warning">${__('Kein Secret gesetzt')}</span>`;

		let status_html;
		if (data.last_received_at) {
			status_html = `<span class="indicator-pill green">${__('Empfange seit Kurzem')}</span>
				<span class="text-muted small" style="margin-left:6px">${__('letzter Empfang: {0}', [fmt_relative(data.last_received_at)])}</span>`;
		} else {
			status_html = `<span class="indicator-pill orange">${__('Noch keine Webhooks empfangen')}</span>`;
		}

		return `
			<div class="ecom-webhook-widget"
			     style="border:1px solid var(--border-color, #d1d8dd);border-radius:4px;padding:12px;margin-top:4px">
				<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
					<div style="font-weight:600">${__('Eingehende Webhooks')}</div>
					<div>${status_html}</div>
				</div>
				<div class="row" style="margin-bottom:6px">
					<div class="col-sm-2 text-muted small">${__('URL')}</div>
					<div class="col-sm-10">
						<code class="ww-url" style="word-break:break-all">${frappe.utils.escape_html(url)}</code>
						<button type="button" class="btn btn-xs btn-default ww-copy-btn" style="margin-left:6px">
							${__('Kopieren')}
						</button>
					</div>
				</div>
				<div class="row" style="margin-bottom:6px">
					<div class="col-sm-2 text-muted small">${__('Secret')}</div>
					<div class="col-sm-10">
						<span class="ww-secret-display">${secret_display}</span>
						<button type="button" class="btn btn-xs btn-default ww-toggle-secret" style="margin-left:6px">
							${data.secret_set ? __('Hinweis anzeigen') : __('Generieren')}
						</button>
					</div>
				</div>
				<div class="text-muted small" style="margin-top:8px">
					ⓘ ${frappe.utils.escape_html(cfg.setup_hint)}
					${__('Events:')} <code>${frappe.utils.escape_html(cfg.events_hint)}</code>
				</div>
				<div style="margin-top:10px">
					<button type="button" class="btn btn-xs btn-default ww-test-btn">
						${__('Test Webhook')}
					</button>
					<a class="btn btn-xs btn-default" href="${cfg.docs_url}" target="_blank" rel="noopener">
						${__('Doku öffnen')}
					</a>
				</div>
			</div>
		`;
	}

	function copy_to_clipboard(text) {
		if (navigator.clipboard && navigator.clipboard.writeText) {
			navigator.clipboard.writeText(text).then(
				() => frappe.show_alert({ message: __('In Zwischenablage kopiert'), indicator: 'green' }),
				() => frappe.show_alert({ message: __('Kopieren fehlgeschlagen'), indicator: 'red' })
			);
			return;
		}
		const ta = document.createElement('textarea');
		ta.value = text;
		document.body.appendChild(ta);
		ta.select();
		try {
			document.execCommand('copy');
			frappe.show_alert({ message: __('In Zwischenablage kopiert'), indicator: 'green' });
		} catch (e) {
			frappe.show_alert({ message: __('Kopieren fehlgeschlagen'), indicator: 'red' });
		}
		document.body.removeChild(ta);
	}

	function fire_test(frm, backend, $host) {
		const cfg = BACKEND_MAP[backend];
		frappe.call({
			method: cfg.module + '.fire_test_webhook',
			freeze: true,
			freeze_message: __('Test-Webhook wird gesendet …'),
			callback: function (r) {
				const msg = (r.message && r.message.message) || (r.message && r.message.success
					? __('Test erfolgreich.')
					: __('Test fehlgeschlagen.'));
				frappe.msgprint({
					title: __('Webhook Test — {0}', [backend]),
					message: msg,
					indicator: (r.message && r.message.success) ? 'green' : 'red',
				});
			},
		});
	}

	function ensure_host(frm, backend) {
		const cfg = BACKEND_MAP[backend];
		// Prefer the existing webhook_url_info HTML field.
		const fd = frm.fields_dict && frm.fields_dict[cfg.host_field];
		if (fd && fd.$wrapper) return fd.$wrapper;
		// Fallback: append a host element to the form layout.
		const $form = $(frm.layout.wrapper);
		let $host = $form.find('.ecom-webhook-widget-host');
		if (!$host.length) {
			$host = $('<div class="ecom-webhook-widget-host" style="margin:12px 0"></div>');
			$form.find('.form-page').first().append($host);
		}
		return $host;
	}

	function render(frm, backend) {
		const cfg = BACKEND_MAP[backend];
		if (!cfg) return;
		if (!frm || !frm.doc) return;
		if (frm.is_new && frm.is_new()) return;

		const $host = ensure_host(frm, backend);
		if (!$host) return;
		$host.html(`<div class="text-muted small">${__('Webhook-Daten werden geladen …')}</div>`);

		frappe.call({
			method: cfg.module + '.get_webhook_info',
			callback: function (r) {
				const data = r.message || { url: '', secret_set: false };
				let secret_visible = false;
				$host.html(build_html(backend, data, secret_visible));

				$host.find('.ww-copy-btn').on('click', function () {
					copy_to_clipboard(data.url || '');
				});
				$host.find('.ww-toggle-secret').on('click', function () {
					if (!data.secret_set) {
						// Suggest generation by directing operator to the wizard.
						frappe.msgprint({
							title: __('Kein Webhook Secret'),
							message: __('Open the Setup Wizard to auto-generate a secure secret, or paste one into the webhook section manually.'),
							indicator: 'orange',
						});
						return;
					}
					secret_visible = !secret_visible;
					$host.find('.ww-secret-display').html(
						secret_visible
							? `<span class="text-muted small">${__('Das Secret ist als Password-Feld gespeichert und kann nur über das Feld unten geändert werden.')}</span>`
							: '••••••••••••••••••••••••••••••••'
					);
				});
				$host.find('.ww-test-btn').on('click', function () {
					fire_test(frm, backend, $host);
				});
			},
			error: function () {
				$host.html(`<div class="text-muted small">${__('Webhook-Daten nicht verfügbar.')}</div>`);
			},
		});
	}

	window.ecom_webhook_widget = { render: render };
})();
