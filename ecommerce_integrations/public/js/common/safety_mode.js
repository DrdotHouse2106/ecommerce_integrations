// One-click Safety Mode quick action for Shopware Setting / Medusa Setting.
// Loaded via `doctype_js` in hooks.py for both forms; the parent setting JS
// calls window.ecom_safety_mode.attach_button(frm, '<Backend>').
//
// Stops outbound writes (item push + on-update sync) and pauses every active
// Catalog Mirror that targets the backend. Inbound webhooks stay live.

(function () {

	const BACKEND_MAP = {
		Shopware: {
			module: 'ecommerce_integrations.shopware6.api.setup',
			enabled_field: 'enable_shopware',
		},
		Medusa: {
			module: 'ecommerce_integrations.medusa.api.setup',
			enabled_field: 'enable_medusa',
		},
	};

	function confirm_and_apply(frm, backend, on_done) {
		const cfg = BACKEND_MAP[backend];
		if (!cfg) return;

		const body = `
			<div>
				<p>${__('Damit wird der automatische Versand gestoppt. Eingehende Webhooks bleiben aktiv.')}</p>
				<p class="text-muted small">${__('Empfohlen vor Datenmigrationen oder bei unsicherem Zustand.')}</p>
				<ul class="text-muted small">
					<li>${__('Item-Push nach {0} aus', [backend])}</li>
					<li>${__('Item-Save-Updates aus')}</li>
					<li>${__('Aktive Catalog Mirrors für {0} werden pausiert', [backend])}</li>
				</ul>
			</div>
		`;

		const d = new frappe.ui.Dialog({
			title: __('Safety Mode aktivieren — {0}', [backend]),
			fields: [{ fieldname: 'info', fieldtype: 'HTML', options: body }],
			primary_action_label: __('Aktivieren'),
			primary_action: function () {
				d.hide();
				frappe.call({
					method: cfg.module + '.apply_safety_mode',
					freeze: true,
					freeze_message: __('Safety Mode wird aktiviert …'),
					callback: function (r) {
						if (r.message && r.message.success) {
							const paused = r.message.mirrors_paused || 0;
							frappe.show_alert({
								message: __('Safety Mode aktiv. {0} Catalog Mirror(s) pausiert.', [paused]),
								indicator: 'orange',
							});
							if (frm && frm.reload_doc) frm.reload_doc();
							if (typeof on_done === 'function') on_done();
						} else {
							frappe.msgprint({
								title: __('Fehler'),
								message: (r.message && r.message.message) || __('Safety Mode konnte nicht aktiviert werden.'),
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

	function attach_button(frm, backend) {
		const cfg = BACKEND_MAP[backend];
		if (!cfg) return;
		if (!frm || !frm.doc) return;
		if (!frm.doc[cfg.enabled_field]) return;
		if (frm.is_new && frm.is_new()) return;

		// Avoid duplicates on refresh re-runs.
		const _i = window.ecom_icon || (() => '');
		const label = _i('shield', 'sm') + ' ' + __('Safety Mode');
		const existing = frm.custom_buttons && frm.custom_buttons[label];
		if (existing) return;

		frm.add_custom_button(label, function () {
			confirm_and_apply(frm, backend);
		});
	}

	window.ecom_safety_mode = {
		attach_button: attach_button,
		confirm_and_apply: confirm_and_apply,
	};
})();
