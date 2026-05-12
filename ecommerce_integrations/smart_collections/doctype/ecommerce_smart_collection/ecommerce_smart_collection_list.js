frappe.listview_settings["Ecommerce Smart Collection"] = {
	add_fields: ["rule_combinator", "last_resolved_count", "last_resolved_at"],

	get_indicator(doc) {
		// ``last_resolved_count`` is the cached result of the last
		// resolver run; surface "never resolved" vs "matched some items"
		// at a glance so the operator can spot dead collections.
		if (!doc.last_resolved_at) {
			return [__("Never resolved"), "grey", "last_resolved_at,is,not set"];
		}
		if (!doc.last_resolved_count) {
			return [__("Matches 0 items"), "orange", "last_resolved_count,=,0"];
		}
		return [
			__("Matches {0}", [doc.last_resolved_count]),
			"green",
			"last_resolved_count,>,0",
		];
	},
};
