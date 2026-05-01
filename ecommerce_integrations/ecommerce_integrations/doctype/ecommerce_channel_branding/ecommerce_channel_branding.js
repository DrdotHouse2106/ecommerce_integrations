// Preview buttons for the Ecommerce Channel Branding form
// Renders PDFs and Emails with the current branding using sample / dummy data

frappe.ui.form.on("Ecommerce Channel Branding", {
	onload(frm) {
		// Populate the channel_name autocomplete with channels from Medusa/Shopware + Sales Orders
		frappe.call({
			method: "ecommerce_integrations.ecommerce_integrations.doctype.ecommerce_channel_branding.ecommerce_channel_branding.get_available_channels",
			callback: (r) => {
				if (r.message && r.message.length) {
					frm.set_df_property("channel_name", "options", r.message.join("\n"));
				}
			},
		});
	},

	refresh(frm) {
		if (frm.is_new()) return;

		frm.add_custom_button(
			__("Order Acknowledgment PDF"),
			() => preview_pdf(frm, "Bestellbestätigung", "Sales Order"),
			__("Preview"),
		);
		frm.add_custom_button(
			__("Order Confirmation PDF"),
			() => preview_pdf(frm, "Auftragsbestätigung", "Sales Order"),
			__("Preview"),
		);
		frm.add_custom_button(
			__("Delivery Note PDF"),
			() => preview_pdf(frm, "Versandbestätigung", "Delivery Note"),
			__("Preview"),
		);
		frm.add_custom_button(
			__("Order Acknowledgment Email"),
			() => preview_email(frm, "acknowledgment"),
			__("Preview"),
		);
		frm.add_custom_button(
			__("Order Confirmation Email"),
			() => preview_email(frm, "confirmation"),
			__("Preview"),
		);
	},
});

function open_html_in_new_tab(html) {
	const w = window.open("", "_blank");
	if (!w) {
		frappe.msgprint({
			title: __("Popup blocked"),
			message: __("Allow popups for this site to see the preview."),
			indicator: "orange",
		});
		return;
	}
	w.document.open();
	w.document.write(html);
	w.document.close();
}

function preview_pdf(frm, print_format, doc_type) {
	frappe.call({
		method: "ecommerce_integrations.ecommerce_integrations.doctype.ecommerce_channel_branding.ecommerce_channel_branding.render_pdf_preview",
		args: {
			channel_name: frm.doc.channel_name,
			print_format: print_format,
			doc_type: doc_type,
		},
		freeze: true,
		freeze_message: __("Rendering preview..."),
		callback: (r) => {
			if (r.message) open_html_in_new_tab(r.message);
		},
	});
}

function preview_email(frm, kind) {
	frappe.call({
		method: "ecommerce_integrations.ecommerce_integrations.doctype.ecommerce_channel_branding.ecommerce_channel_branding.render_email_preview",
		args: { channel_name: frm.doc.channel_name, kind: kind },
		freeze: true,
		freeze_message: __("Rendering preview..."),
		callback: (r) => {
			if (r.message) open_html_in_new_tab(r.message);
		},
	});
}
