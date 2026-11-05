// Customer order comment as a prominent fulfillment hint.
// Loaded for Sales Order, Delivery Note, Pick List and Packing Slip (see hooks.py).
// The comment is stored as plain text; it is escaped here before rendering.
frappe.ui.form.on(cur_frm.doctype, {
	refresh(frm) {
		const comment = (frm.doc.ecommerce_customer_comment || "").trim();
		if (!comment) {
			return;
		}

		const highlighted = !!frm.doc.ecommerce_comment_highlight;
		const title = highlighted
			? __("Wichtiger Hinweis zur Bestellung")
			: __("Kundenkommentar");
		const text_style = highlighted
			? "font-size: 1.25em; font-weight: 700;"
			: "font-size: 1.1em; font-weight: 600;";

		frm.dashboard.set_headline_alert(
			`<div class="ecommerce-customer-comment">
				<div style="margin-bottom: 4px;"><strong>${frappe.utils.escape_html(title)}:</strong></div>
				<div style="white-space: pre-line; ${text_style}">${frappe.utils.escape_html(comment)}</div>
			</div>`,
			highlighted ? "red" : "yellow",
			true,
		);
	},
});
