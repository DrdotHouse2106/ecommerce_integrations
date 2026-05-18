// 5-step setup wizard for the Shopware Setting form.
// Loaded via `doctype_js` in hooks.py; the parent setting JS adds a
// "wand-sparkles" Setup-Wizard button that calls
// window.shopware6_setup_wizard.open(frm).
//
// Plain-language, German-first. All user-visible strings go through __().
// Talks to ecommerce_integrations.shopware6.api.setup for the heavy lifting
// (test connection round-trip, webhook test, final apply).

(function () {

	const _i = window.ecom_icon || (() => '');
	const API_MODULE = 'ecommerce_integrations.shopware6.api.setup';
	const TOTAL_STEPS = 5;

	const STEP_TITLES = () => ([
		__('Verbindung'),
		__('Verbindung testen'),
		__('Synchronisations-Modus wählen'),
		__('Webhook einrichten'),
		__('Aktivieren'),
	]);

	function step_label(idx) {
		return __('Shopware 6 einrichten — Schritt {0} von {1}: {2}',
			[idx + 1, TOTAL_STEPS, STEP_TITLES()[idx]]);
	}

	function progress_html(current_idx) {
		const pct = Math.round(((current_idx + 1) / TOTAL_STEPS) * 100);
		let dots = '';
		for (let i = 0; i < TOTAL_STEPS; i++) {
			const filled = i <= current_idx ? '#5e64ff' : '#dee2e6';
			dots += `<span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:${filled};margin-right:4px"></span>`;
		}
		return `
			<div style="margin-bottom:10px">
				<div style="height:4px;background:#eef0f3;border-radius:2px;overflow:hidden">
					<div style="width:${pct}%;height:100%;background:#5e64ff"></div>
				</div>
				<div style="margin-top:6px;color:#6c757d;font-size:12px">${dots} ${pct}%</div>
			</div>
		`;
	}

	// State carried across steps.
	function make_state(frm) {
		return {
			shop_url: frm.doc.shop_url || '',
			grant_type: frm.doc.grant_type || 'resource_owner',
			client_id: frm.doc.client_id || '',
			client_secret: '',
			api_username: frm.doc.api_username || '',
			api_password: '',
			sync_mode: 'receive',
			webhook_secret: '',
			tested_ok: false,
		};
	}

	function open(frm) {
		if (!frm) return;
		const state = make_state(frm);
		render_step(frm, state, 0);
	}

	function render_step(frm, state, idx) {
		const renderers = [step1_connection, step2_test, step3_mode, step4_webhook, step5_activate];
		renderers[idx](frm, state, idx);
	}

	// ---- Step 1: Welcome & Connection -------------------------------------
	function step1_connection(frm, state, idx) {
		const fields = [
			{ fieldname: 'info', fieldtype: 'HTML',
				options: `${progress_html(idx)}<p>${__('Trage deine Shopware Admin API Credentials ein. Du findest sie in Shopware: Einstellungen → System → Integrationen.')}</p>` },
			{ fieldname: 'shop_url', fieldtype: 'Data', label: __('Shop URL'), reqd: 1,
				default: state.shop_url,
				description: __('z. B. https://yourshop.com') },
			{ fieldname: 'grant_type', fieldtype: 'Select', label: __('Authentifizierung'),
				options: 'resource_owner\nuser_credentials', default: state.grant_type,
				description: __('Empfohlen: resource_owner (Integration mit Client ID + Client Secret).') },
			{ fieldname: 'section_int', fieldtype: 'Section Break',
				depends_on: 'eval:doc.grant_type==="resource_owner"' },
			{ fieldname: 'client_id', fieldtype: 'Data', label: __('Client ID'),
				default: state.client_id,
				mandatory_depends_on: 'eval:doc.grant_type==="resource_owner"' },
			{ fieldname: 'client_secret', fieldtype: 'Password', label: __('Client Secret'),
				mandatory_depends_on: 'eval:doc.grant_type==="resource_owner"' },
			{ fieldname: 'section_user', fieldtype: 'Section Break',
				depends_on: 'eval:doc.grant_type==="user_credentials"' },
			{ fieldname: 'api_username', fieldtype: 'Data', label: __('API Username'),
				default: state.api_username,
				mandatory_depends_on: 'eval:doc.grant_type==="user_credentials"' },
			{ fieldname: 'api_password', fieldtype: 'Password', label: __('API Password'),
				mandatory_depends_on: 'eval:doc.grant_type==="user_credentials"' },
		];

		const d = new frappe.ui.Dialog({
			title: step_label(idx),
			size: 'large',
			fields: fields,
			primary_action_label: __('Weiter'),
			primary_action: function (v) {
				state.shop_url = v.shop_url || '';
				state.grant_type = v.grant_type || 'resource_owner';
				state.client_id = v.client_id || '';
				if (v.client_secret) state.client_secret = v.client_secret;
				state.api_username = v.api_username || '';
				if (v.api_password) state.api_password = v.api_password;
				if (!state.shop_url) {
					frappe.show_alert({ message: __('Bitte Shop URL eingeben.'), indicator: 'red' });
					return;
				}
				d.hide();
				// Persist credentials before testing so test_connection can use them.
				persist_connection(frm, state).then(() => render_step(frm, state, idx + 1));
			},
		});
		d.set_secondary_action_label(__('Abbrechen'));
		d.set_secondary_action(function () { d.hide(); });
		d.show();
	}

	function persist_connection(frm, state) {
		return new Promise(function (resolve) {
			// Save credentials to the Setting doc using a partial apply so test_connection
			// can read them. We do NOT enable the integration yet.
			const payload = {
				shop_url: state.shop_url,
				grant_type: state.grant_type,
				client_id: state.client_id,
				client_secret: state.client_secret,
				api_username: state.api_username,
				api_password: state.api_password,
				sync_mode: 'receive',  // safe default until step 3
			};
			// Patch the form values directly + save (without enabling).
			frm.set_value('shop_url', state.shop_url);
			frm.set_value('grant_type', state.grant_type);
			if (state.client_id) frm.set_value('client_id', state.client_id);
			if (state.client_secret) frm.set_value('client_secret', state.client_secret);
			if (state.api_username) frm.set_value('api_username', state.api_username);
			if (state.api_password) frm.set_value('api_password', state.api_password);
			frm.save().then(() => resolve()).catch(() => resolve());
		});
	}

	// ---- Step 2: Test connection ------------------------------------------
	function step2_test(frm, state, idx) {
		const d = new frappe.ui.Dialog({
			title: step_label(idx),
			size: 'large',
			fields: [{ fieldname: 'body', fieldtype: 'HTML',
				options: `${progress_html(idx)}<div class="ww-test-body">${__('Verbindung wird getestet …')}</div>` }],
			primary_action_label: __('Weiter'),
			primary_action: function () {
				if (!state.tested_ok) {
					frappe.show_alert({ message: __('Bitte zuerst eine erfolgreiche Verbindung herstellen.'), indicator: 'orange' });
					return;
				}
				d.hide();
				render_step(frm, state, idx + 1);
			},
		});
		d.set_secondary_action_label(__('Zurück'));
		d.set_secondary_action(function () { d.hide(); render_step(frm, state, idx - 1); });
		d.show();

		function run_test() {
			const $body = d.$wrapper.find('.ww-test-body');
			$body.html(`<div class="text-muted small">${__('Verbindung wird getestet …')}</div>`);
			frm.call({
				method: 'test_connection',
				doc: frm.doc,
				callback: function (r) {
					const ok = r.message && (r.message.success || r.message.status === 'success');
					if (ok) {
						state.tested_ok = true;
						const channels = r.message.sales_channels;
						const msg = (channels !== undefined)
							? __('Verbindung erfolgreich. {0} Sales Channels gefunden.', [channels])
							: __('Verbindung erfolgreich.');
						$body.html(
							`<div class="alert alert-success" style="margin:0;display:flex;align-items:center;gap:6px">${_i('circle-check', 'sm')} <span>${frappe.utils.escape_html(msg)}</span></div>`
							+ `<div style="margin-top:10px"><button type="button" class="btn btn-default btn-sm retest-btn">${__('Erneut testen')}</button></div>`
						);
					} else {
						state.tested_ok = false;
						const err = (r.message && (r.message.error || r.message.message))
							|| __('Verbindung fehlgeschlagen. Bitte URL und Zugangsdaten prüfen.');
						$body.html(
							`<div class="alert alert-danger" style="margin:0;display:flex;align-items:center;gap:6px">${_i('circle-x', 'sm')} <span>${frappe.utils.escape_html(err)}</span></div>`
							+ `<div style="margin-top:10px"><button type="button" class="btn btn-default btn-sm retest-btn">${__('Erneut testen')}</button></div>`
						);
					}
					$body.find('.retest-btn').on('click', run_test);
				},
			});
		}
		run_test();
	}

	// ---- Step 3: Sync mode -------------------------------------------------
	function step3_mode(frm, state, idx) {
		const d = new frappe.ui.Dialog({
			title: step_label(idx),
			size: 'large',
			fields: [
				{ fieldname: 'info', fieldtype: 'HTML', options: progress_html(idx) },
				{ fieldname: 'sync_mode', fieldtype: 'Select', label: __('Modus'),
					options: [
						'receive', 'manual', 'full',
					].join('\n'),
					default: state.sync_mode,
				},
				{ fieldname: 'mode_help', fieldtype: 'HTML',
					options: `<ul>
						<li><b>receive</b> — ${__('Empfang aus Shopware: Bestellungen, Kunden, Bestand kommen rein in ERPNext (sicher empfohlen für den Einstieg).')}</li>
						<li><b>manual</b> — ${__('Empfang + manueller Versand: Empfang automatisch, Produkt-/Kategorie-Sync läuft nur wenn du auf "Sync starten" klickst.')}</li>
						<li><b>full</b> — ${__('Vollautomatisch (Fortgeschritten): Item-Saves pushen sofort nach Shopware, Catalog Mirror Cron läuft stündlich (mit Bedacht aktivieren).')}</li>
					</ul>` },
			],
			primary_action_label: __('Weiter'),
			primary_action: function (v) {
				state.sync_mode = v.sync_mode || 'receive';
				d.hide();
				render_step(frm, state, idx + 1);
			},
		});
		d.set_secondary_action_label(__('Zurück'));
		d.set_secondary_action(function () { d.hide(); render_step(frm, state, idx - 1); });
		d.show();
	}

	// ---- Step 4: Webhook setup --------------------------------------------
	function step4_webhook(frm, state, idx) {
		const d = new frappe.ui.Dialog({
			title: step_label(idx),
			size: 'large',
			fields: [{ fieldname: 'body', fieldtype: 'HTML',
				options: `${progress_html(idx)}<div class="webhook-body">${__('Lädt …')}</div>` }],
			primary_action_label: __('Weiter'),
			primary_action: function () {
				d.hide();
				render_step(frm, state, idx + 1);
			},
		});
		d.set_secondary_action_label(__('Zurück'));
		d.set_secondary_action(function () { d.hide(); render_step(frm, state, idx - 1); });
		d.show();

		frappe.call({
			method: API_MODULE + '.get_webhook_info',
			callback: function (r) {
				const info = r.message || { url: '', secret_set: false };
				const url = info.url || '';
				const has_secret = !!info.secret_set;
				let secret_value = state.webhook_secret || '';
				if (!has_secret && !secret_value) {
					secret_value = generate_secret();
					state.webhook_secret = secret_value;
				}
				const events_hint = 'order.placed, order.written, customer.written, product_stock.written';
				const $body = d.$wrapper.find('.webhook-body');
				$body.html(`
					<p>${__('In Shopware: Einstellungen → System → Webhooks → "Webhook hinzufügen". Klebe URL und Secret in die Felder. Wähle Events: {0}.', [events_hint])}</p>
					<div style="margin:8px 0">
						<label class="small text-muted">${__('Webhook URL')}</label>
						<div style="display:flex;gap:6px;align-items:center">
							<code style="flex:1;word-break:break-all">${frappe.utils.escape_html(url)}</code>
							<button type="button" class="btn btn-xs btn-default copy-url-btn">${__('Webhook URL kopieren')}</button>
						</div>
					</div>
					<div style="margin:8px 0">
						<label class="small text-muted">${__('Webhook Secret')}</label>
						<div style="display:flex;gap:6px;align-items:center">
							<code class="secret-display" style="flex:1;word-break:break-all">${has_secret && !state.webhook_secret ? '••••••••••••••••••••••••••••••••' : frappe.utils.escape_html(secret_value)}</code>
							<button type="button" class="btn btn-xs btn-default copy-secret-btn">${__('Secret kopieren')}</button>
							<button type="button" class="btn btn-xs btn-default toggle-secret-btn">${__('Zeigen/verbergen')}</button>
						</div>
						${has_secret && !state.webhook_secret
							? `<div class="text-muted small">${__('Ein Secret ist bereits gesetzt. Du kannst es behalten oder hier neu generieren.')}</div>`
							: ''}
					</div>
					<div style="margin-top:10px">
						<button type="button" class="btn btn-default btn-sm test-webhook-btn">${__('Test Webhook')}</button>
						<button type="button" class="btn btn-default btn-sm regen-secret-btn">${__('Neues Secret generieren')}</button>
					</div>
					<div class="test-result text-muted small" style="margin-top:8px"></div>
				`);

				let secret_visible = !has_secret || !!state.webhook_secret;
				$body.find('.copy-url-btn').on('click', () => copy_to_clipboard(url));
				$body.find('.copy-secret-btn').on('click', () => {
					if (state.webhook_secret) copy_to_clipboard(state.webhook_secret);
					else frappe.msgprint(__('Kein Klartext-Secret verfügbar (bereits gespeichert).'));
				});
				$body.find('.toggle-secret-btn').on('click', () => {
					secret_visible = !secret_visible;
					$body.find('.secret-display').text(
						secret_visible
							? (state.webhook_secret || (has_secret ? '••••••••••••••••••••••••••••••••' : ''))
							: '••••••••••••••••••••••••••••••••'
					);
				});
				$body.find('.regen-secret-btn').on('click', () => {
					state.webhook_secret = generate_secret();
					$body.find('.secret-display').text(state.webhook_secret);
					secret_visible = true;
				});
				$body.find('.test-webhook-btn').on('click', () => {
					const $r = $body.find('.test-result');
					$r.text(__('Test läuft …'));
					frappe.call({
						method: API_MODULE + '.fire_test_webhook',
						callback: function (rr) {
							const ok = rr.message && rr.message.success;
							$r.html(ok
								? `<span class="text-success" style="display:inline-flex;align-items:center;gap:4px">${_i('circle-check', 'xs')} ${frappe.utils.escape_html(rr.message.message || __('Erfolgreich.'))}</span>`
								: `<span class="text-danger" style="display:inline-flex;align-items:center;gap:4px">${_i('circle-x', 'xs')} ${frappe.utils.escape_html((rr.message && rr.message.message) || __('Test fehlgeschlagen.'))}</span>`
							);
						},
					});
				});
			},
		});
	}

	// ---- Step 5: Activate --------------------------------------------------
	function step5_activate(frm, state, idx) {
		const mode_label_map = {
			receive: __('Empfang aus Shopware'),
			manual: __('Empfang + manueller Versand'),
			full: __('Vollautomatisch (Fortgeschritten)'),
		};
		const mode_label = mode_label_map[state.sync_mode] || state.sync_mode;

		const body = `
			${progress_html(idx)}
			<p>${__('Wenn du auf "Aktivieren" klickst, wird:')}</p>
			<ul>
				<li>${__('Shopware-Integration eingeschaltet')}</li>
				<li>${__('Modus "{0}" angewendet', [mode_label])}</li>
				<li>${__('Webhooks werden ab sofort verarbeitet')}</li>
			</ul>
		`;
		const d = new frappe.ui.Dialog({
			title: step_label(idx),
			size: 'large',
			fields: [{ fieldname: 'info', fieldtype: 'HTML', options: body }],
			primary_action_label: __('Aktivieren'),
			primary_action: function () {
				frappe.call({
					method: API_MODULE + '.apply_setup_wizard',
					args: {
						payload: JSON.stringify({
							shop_url: state.shop_url,
							grant_type: state.grant_type,
							client_id: state.client_id,
							client_secret: state.client_secret,
							api_username: state.api_username,
							api_password: state.api_password,
							sync_mode: state.sync_mode,
							webhook_secret: state.webhook_secret,
						}),
					},
					freeze: true,
					freeze_message: __('Aktivierung läuft …'),
					callback: function (r) {
						if (r.message && r.message.success) {
							d.hide();
							frappe.show_alert({
								message: __('Shopware-Integration aktiviert.'),
								indicator: 'green',
							});
							frm.reload_doc();
						} else {
							frappe.msgprint({
								title: __('Fehler'),
								message: (r.message && r.message.message) || __('Aktivierung fehlgeschlagen.'),
								indicator: 'red',
							});
						}
					},
				});
			},
		});
		d.set_secondary_action_label(__('Abbrechen'));
		d.set_secondary_action(function () { d.hide(); });
		d.show();
	}

	// ---- Helpers -----------------------------------------------------------
	function generate_secret() {
		const arr = new Uint8Array(32);
		try {
			window.crypto.getRandomValues(arr);
		} catch (e) {
			for (let i = 0; i < arr.length; i++) arr[i] = Math.floor(Math.random() * 256);
		}
		let s = '';
		const alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789';
		for (let i = 0; i < arr.length; i++) s += alphabet[arr[i] % alphabet.length];
		return s;
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
		try { document.execCommand('copy'); frappe.show_alert({ message: __('In Zwischenablage kopiert'), indicator: 'green' }); }
		catch (e) { frappe.show_alert({ message: __('Kopieren fehlgeschlagen'), indicator: 'red' }); }
		document.body.removeChild(ta);
	}

	window.shopware6_setup_wizard = { open: open };
})();
