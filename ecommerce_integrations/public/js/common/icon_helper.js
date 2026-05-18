// Shared SVG-sprite icon helper for ecommerce_integrations admin UI.
//
// Frappe v16 loads the Lucide icon sprite globally on every desk page,
// so any ``#icon-${name}`` reference works without bundling. This file
// exposes one terse global so the setting/wizard/widget JS files can
// stop using emojis (which render inconsistently across OSes and never
// inherit the theme's text color).
//
// Lookup the icon name at https://lucide.dev — every Lucide icon ships
// in Frappe v16's sprite. Common ones used in this codebase:
//
//   plus, pencil, move, minus, trash-2, link, check, x, search, filter,
//   triangle-alert, circle-alert, circle-check, circle-x, circle-slash,
//   zap, wand-sparkles, shield, hard-hat, refresh-cw, eye, eye-off,
//   list, info, settings, external-link, copy.
//
// Sizes (Frappe convention): 'xs' | 'sm' | 'md' | 'lg' | 'xl'.

(function () {
	function icon(name, size, color) {
		const cls = 'icon icon-' + (size || 'sm');
		const style = color
			? `color:${color};vertical-align:middle`
			: 'vertical-align:middle';
		return `<svg class="${cls}" style="${style}"><use href="#icon-${name}"></use></svg>`;
	}
	window.ecom_icon = icon;
})();
