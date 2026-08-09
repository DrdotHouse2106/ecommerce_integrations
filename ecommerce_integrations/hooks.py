from . import __version__ as app_version

app_name = "ecommerce_integrations"
app_title = "Ecommerce Integrations"
app_publisher = "Frappe"
app_description = "Ecommerce integrations for ERPNext"
app_icon = "octicon octicon-file-directory"
app_color = "grey"
app_email = "developers@frappe.io"
app_license = "GNU GPL v3.0"
required_apps = ["frappe/erpnext"]

# Includes in <head>
# ------------------

# include js, css files in header of desk.html
# app_include_css = "/assets/ecommerce_integrations/css/ecommerce_integrations.css"
# app_include_js = "/assets/ecommerce_integrations/js/ecommerce_integrations.js"

# include js, css files in header of web template
# web_include_css = "/assets/ecommerce_integrations/css/ecommerce_integrations.css"
# web_include_js = "/assets/ecommerce_integrations/js/ecommerce_integrations.js"

# include custom scss in every website theme (without file extension ".scss")
# website_theme_scss = "ecommerce_integrations/public/scss/website"

# include js, css files in header of web form
# webform_include_js = {"doctype": "public/js/doctype.js"}
# webform_include_css = {"doctype": "public/css/doctype.css"}

# include js in page
# page_js = {"page" : "public/js/file.js"}

# include js in doctype views
doctype_js = {
	"Shopify Settings": "public/js/shopify/old_settings.js",
	"Sales Order": [
		"public/js/unicommerce/sales_order.js",
		"public/js/common/ecommerce_transactions.js",
	],
	"Sales Invoice": [
		"public/js/unicommerce/sales_invoice.js",
		"public/js/common/ecommerce_transactions.js",
	],
	"Item": [
		"public/js/unicommerce/item.js",
		"public/js/ai_description/item.js",
		"public/js/shopware6/item.js",
		"public/js/medusa/item.js",
	],
	"Item Group": [
		"public/js/shopware6/item_group.js",
	],
	"Stock Entry": "public/js/unicommerce/stock_entry.js",
	"Pick List": "public/js/unicommerce/pick_list.js",
	"Medusa Setting": [
		"public/js/common/icon_helper.js",
		"public/js/smart_collections/setting_widget.js",
		"public/js/catalog_mirror/setting_widget.js",
		"public/js/product_sync/setting_widget.js",
		"public/js/common/health_banner.js",
		"public/js/common/safety_mode.js",
		"public/js/common/webhook_widget.js",
		"public/js/medusa/setup_wizard.js",
	],
	"Shopware Setting": [
		"public/js/common/icon_helper.js",
		"public/js/smart_collections/setting_widget.js",
		"public/js/catalog_mirror/setting_widget.js",
		"public/js/product_sync/setting_widget.js",
		"public/js/common/health_banner.js",
		"public/js/common/safety_mode.js",
		"public/js/common/webhook_widget.js",
		"public/js/shopware6/setup_wizard.js",
	],
}
# doctype_list_js = {"doctype" : "public/js/doctype_list.js"}
# doctype_tree_js = {"doctype" : "public/js/doctype_tree.js"}
# doctype_calendar_js = {"doctype" : "public/js/doctype_calendar.js"}

# Home Pages
# ----------

# application home page (will override Website Settings)
# home_page = "login"

# website user home page (by Role)
# role_home_page = {
# 	"Role": "home_page"
# }

# Generators
# ----------

# automatically create page for each record of this doctype
# website_generators = ["Web Page"]

# Installation
# ------------

# before_install = "ecommerce_integrations.install.before_install"
after_install = "ecommerce_integrations.install.after_install"


before_uninstall = "ecommerce_integrations.uninstall.before_uninstall"

# Desk Notifications
# ------------------
# See frappe.core.notifications.get_notification_config

# notification_config = "ecommerce_integrations.notifications.get_notification_config"

# Permissions
# -----------
# Permissions evaluated in scripted ways

# permission_query_conditions = {
# 	"Event": "frappe.desk.doctype.event.event.get_permission_query_conditions",
# }
#
# has_permission = {
# 	"Event": "frappe.desk.doctype.event.event.has_permission",
# }

# DocType Class
# ---------------
# Override standard doctype classes

override_doctype_class = {
	"Notification": "ecommerce_integrations.ecommerce_integrations.channel_aware_notification.ChannelAwareNotification",
}

# Jinja — expose channel branding to templates (Notifications, Print Formats)
# Templates run in Frappe's Jinja sandbox; only methods listed here are callable.
jinja = {
	"methods": [
		"ecommerce_integrations.ecommerce_integrations.doctype.ecommerce_channel_branding.ecommerce_channel_branding.get_branding",
		"ecommerce_integrations.ecommerce_integrations.doctype.ecommerce_channel_branding.ecommerce_channel_branding.build_greeting_context",
		"ecommerce_integrations.ecommerce_integrations.doctype.ecommerce_channel_branding.ecommerce_channel_branding.render_branding_text",
		"ecommerce_integrations.ecommerce_integrations.doctype.ecommerce_channel_branding.ecommerce_channel_branding.get_invoice_payment_context",
		"ecommerce_integrations.ecommerce_integrations.doctype.ecommerce_channel_branding.ecommerce_channel_branding.get_default_bank_info",
	]
}

# Fixtures — shipped with the plugin and synced on `bench migrate`
fixtures = [
	{
		"dt": "Notification",
		"filters": [["module", "=", "ecommerce_integrations"]],
	},
]

# Document Events
# ---------------
# Hook on document methods and events

doc_events = {
	"Item": {
		"after_insert": [
			"ecommerce_integrations.shopify.product.upload_erpnext_item",
			# Shopware6: Use bulk sync queue to prevent crashes during bulk updates
			"ecommerce_integrations.shopware6.bulk_sync.queue_item_for_sync",
			# RAG: Sync items to Vector Search
			"ecommerce_integrations.rag.bulk_sync.queue_item_for_sync",
			"ecommerce_integrations.medusa.bulk_sync.queue_item_for_sync",
		],
		"on_update": [
			"ecommerce_integrations.shopify.product.upload_erpnext_item",
			# Shopware6: Use bulk sync queue to prevent crashes during bulk updates
			"ecommerce_integrations.shopware6.bulk_sync.queue_item_for_sync",
			"ecommerce_integrations.shopware6.bulk_sync.queue_properties_for_sync",
			# RAG: Sync items to Vector Search
			"ecommerce_integrations.rag.bulk_sync.queue_item_for_sync",
			"ecommerce_integrations.medusa.bulk_sync.queue_item_for_sync",
			# Smart Collections: drop the visibility cache so the next
			# product-sync cycle picks up changed item-group / property /
			# manufacturer / brand membership.
			"ecommerce_integrations.smart_collections.hooks.invalidate_visibility_cache",
		],
		"on_trash": [
			# RAG: Delete items from Vector Search
			"ecommerce_integrations.rag.product_export.delete_item_from_rag",
			# Shopware6: Deactivate product in Shopware and cleanup Ecommerce Item
			"ecommerce_integrations.shopware6.bulk_sync.queue_item_delete_for_sync",
			# Medusa: set product status=draft (keep due to order refs)
			"ecommerce_integrations.medusa.product_export.deactivate_item_in_medusa",
		],
		"validate": [
			"ecommerce_integrations.utils.taxation.validate_tax_template",
			"ecommerce_integrations.unicommerce.product.validate_item",
		],
	},
	# Shopware6: Sync Item Group changes (description, shopware_active, SEO) to Shopware categories
	"Item Group": {
		"on_update": [
			"ecommerce_integrations.shopware6.bulk_sync.queue_item_group_for_sync",
			"ecommerce_integrations.medusa.product_export.sync_item_group_to_medusa",
			# Smart Collections: re-resolves on next sync need a clean cache.
			"ecommerce_integrations.smart_collections.hooks.invalidate_visibility_cache",
		],
		"after_rename": "ecommerce_integrations.shopware6.bulk_sync.queue_item_group_rename_for_sync",
		"on_trash": "ecommerce_integrations.shopware6.bulk_sync.queue_item_group_delete_for_sync",
	},
	"Item Ecommerce Property": {
		"after_insert": "ecommerce_integrations.smart_collections.hooks.invalidate_visibility_cache",
		"on_update": "ecommerce_integrations.smart_collections.hooks.invalidate_visibility_cache",
		"on_trash": "ecommerce_integrations.smart_collections.hooks.invalidate_visibility_cache",
	},
	"Ecommerce Smart Collection": {
		"on_update": "ecommerce_integrations.smart_collections.hooks.invalidate_visibility_cache",
		"on_trash": "ecommerce_integrations.smart_collections.hooks.invalidate_visibility_cache",
	},
	# File: catch image attachments added/removed without an Item save so
	# gallery-image changes still trigger a per-channel resync. The handler
	# filters non-image and non-Item-attached Files itself, then forwards to
	# both Shopware and Medusa bulk queues.
	"File": {
		"after_insert": "ecommerce_integrations.ecommerce_integrations.image_sync.queue_parent_item_for_sync",
		"on_trash": "ecommerce_integrations.ecommerce_integrations.image_sync.queue_parent_item_for_sync",
	},
	"Sales Order": {
		"on_submit": "ecommerce_integrations.shopware6.status_sync.on_sales_order_submit",
		"on_update_after_submit": "ecommerce_integrations.unicommerce.order.update_shipping_info",
		"on_cancel": [
			"ecommerce_integrations.unicommerce.status_updater.ignore_pick_list_on_sales_order_cancel",
			"ecommerce_integrations.shopware6.status_sync.on_sales_order_cancel",
			"ecommerce_integrations.medusa.status_sync.on_sales_order_cancel",
		],
	},
	# Mirror our friendly ``Customer.leitweg_id`` field into Alyf's
	# canonical ``electronic_address`` + EAS scheme ``0204`` so the
	# eu_einvoice XRechnung renderer picks up the Leitweg-ID without
	# the operator having to maintain two parallel fields.
	"Customer": {
		"before_save": "ecommerce_integrations.shopware6.customer.sync.mirror_leitweg_into_electronic_address",
		"after_insert": "ecommerce_integrations.shopware6.customer.sync.mirror_leitweg_into_electronic_address",
	},
	"Delivery Note": {
		"before_insert": "ecommerce_integrations.ecommerce_integrations.channel_propagation.propagate_to_delivery_note",
		"validate": "ecommerce_integrations.ecommerce_integrations.channel_propagation.propagate_to_delivery_note",
		"on_submit": [
			"ecommerce_integrations.shopware6.status_sync.on_delivery_note_submit",
			"ecommerce_integrations.medusa.status_sync.on_delivery_note_submit",
		],
		"on_cancel": "ecommerce_integrations.shopware6.status_sync.on_delivery_note_cancel",
	},
	"Payment Entry": {
		"before_insert": "ecommerce_integrations.ecommerce_integrations.channel_propagation.propagate_to_payment_entry",
		"validate": "ecommerce_integrations.ecommerce_integrations.channel_propagation.propagate_to_payment_entry",
		"on_submit": "ecommerce_integrations.shopware6.status_sync.on_payment_entry_submit",
	},
	"Stock Entry": {
		"validate": "ecommerce_integrations.unicommerce.grn.validate_stock_entry_for_grn",
		"on_submit": [
			"ecommerce_integrations.unicommerce.grn.upload_grn",
			"ecommerce_integrations.shopware6.inventory.update_stock_on_stock_entry",
			"ecommerce_integrations.medusa.inventory.update_stock_on_stock_entry",
		],
		"on_cancel": "ecommerce_integrations.unicommerce.grn.prevent_grn_cancel",
	},
	# Shopware6: Sync stock after Stock Reconciliation
	"Stock Reconciliation": {
		"on_submit": [
			"ecommerce_integrations.shopware6.inventory.update_stock_on_stock_reconciliation",
			"ecommerce_integrations.medusa.inventory.update_stock_on_stock_reconciliation",
		],
	},
	"Item Price": {
		"on_change": [
			"ecommerce_integrations.utils.price_list.discard_item_prices",
			"ecommerce_integrations.shopware6.bulk_sync.queue_price_for_sync",
			"ecommerce_integrations.medusa.bulk_sync.queue_price_for_sync",
		],
	},
	"Pick List": {"validate": "ecommerce_integrations.unicommerce.pick_list.validate"},
	"Sales Invoice": {
		"before_insert": "ecommerce_integrations.ecommerce_integrations.channel_propagation.propagate_to_sales_invoice",
		"validate": "ecommerce_integrations.ecommerce_integrations.channel_propagation.propagate_to_sales_invoice",
		"on_submit": [
			"ecommerce_integrations.unicommerce.invoice.on_submit",
			"ecommerce_integrations.shopware6.status_sync.on_sales_invoice_submit",
		],
		"on_cancel": "ecommerce_integrations.unicommerce.invoice.on_cancel",
	},
}

# Scheduled Tasks
# ---------------

scheduler_events = {
	"all": ["ecommerce_integrations.shopify.inventory.update_inventory_on_shopify"],
	"daily": [],
	"daily_long": ["ecommerce_integrations.zenoti.doctype.zenoti_settings.zenoti_settings.sync_stocks"],
	"hourly": [
		"ecommerce_integrations.shopify.order.sync_old_orders",
		"ecommerce_integrations.amazon.doctype.amazon_sp_api_settings.amazon_sp_api_settings.schedule_get_order_details",
		# AI Description: Scheduled batch processing
		"ecommerce_integrations.ai_description.scheduler.process_batch_descriptions",
		# Medusa: safety-net index-sync if last_synced_at is stale
		"ecommerce_integrations.medusa.product_export.ensure_index_fresh",
		# Smart Collections: re-flag targets stuck in 'running' past the heartbeat timeout
		"ecommerce_integrations.smart_collections.tasks.recover_stale_targets",
		# Catalog Mirror: re-flag mirrors stuck in 'running' past heartbeat
		"ecommerce_integrations.catalog_mirror.tasks.recover_stale_mirrors",
		# Product Sync: re-flag apply-live runs stuck in 'running' past heartbeat
		# (worker OOM/SIGKILL/restart leaves the parent sync's claim dangling).
		"ecommerce_integrations.product_sync.tasks.recover_stale_product_syncs",
	],
	"hourly_long": [
		"ecommerce_integrations.zenoti.doctype.zenoti_settings.zenoti_settings.sync_invoices",
		"ecommerce_integrations.unicommerce.product.upload_new_items",
		"ecommerce_integrations.unicommerce.status_updater.update_sales_order_status",
		"ecommerce_integrations.unicommerce.status_updater.update_shipping_package_status",
		# Smart Collections: re-resolve and push category memberships to backends.
		"ecommerce_integrations.smart_collections.tasks.sync_due_collections",
		# Catalog Mirror: walk + diff + apply every active mirror against its backend
		"ecommerce_integrations.catalog_mirror.tasks.sync_due_mirrors",
	],
	"weekly": [],
	"monthly": [],
	"cron": {
		# Every minute: drain the Redis-backed bulk-sync queues. When bulk
		# mode activates (mass imports), the doc-event hooks only queue and
		# deliberately skip enqueuing a processor job — this tick is what
		# picks the work up once the save burst has quieted down. Without
		# it the Medusa queue (1 h TTL) silently expires after an import.
		"* * * * *": [
			"ecommerce_integrations.medusa.bulk_sync.check_and_process_queue",
			"ecommerce_integrations.rag.bulk_sync.check_and_process_queue",
		],
		# Every five minutes
		"*/5 * * * *": [
			"ecommerce_integrations.unicommerce.order.sync_new_orders",
			"ecommerce_integrations.unicommerce.inventory.update_inventory_on_unicommerce",
			"ecommerce_integrations.unicommerce.delivery_note.prepare_delivery_note",
		],
		# Product Sync: per-doc cron dispatcher for push direction.
		# Pull Sync: same pattern for the inverse direction (Backend →
		# ERPNext orders/customers/inventory) so both pulls and pushes
		# share one audit trail (``Ecommerce Sync Run``) and the same
		# 15-minute tick cadence.
		"*/15 * * * *": [
			"ecommerce_integrations.product_sync.tasks.dispatch_due_syncs",
			"ecommerce_integrations.product_sync.pull_tasks.dispatch_due_pulls",
		],
	},
}


# bootinfo - hide old doctypes
extend_bootinfo = "ecommerce_integrations.boot.boot_session"

# Testing
# -------

before_tests = "ecommerce_integrations.utils.before_test.before_tests"

# Overriding Methods
# ------------------------------
#
# override_whitelisted_methods = {
# 	"frappe.desk.doctype.event.event.get_events": "ecommerce_integrations.event.get_events"
# }
#
# each overriding function accepts a `data` argument;
# generated from the base implementation of the doctype dashboard,
# along with any modifications made in other Frappe apps
# override_doctype_dashboards = {
# 	"Task": "ecommerce_integrations.task.get_dashboard_data"
# }

# exempt linked doctypes from being automatically cancelled
#
# auto_cancel_exempted_doctypes = ["Auto Repeat"]


# User Data Protection
# --------------------

# user_data_fields = [
# 	{
# 		"doctype": "{doctype_1}",
# 		"filter_by": "{filter_by}",
# 		"redact_fields": ["{field_1}", "{field_2}"],
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_2}",
# 		"filter_by": "{filter_by}",
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_3}",
# 		"strict": False,
# 	},
# 	{
# 		"doctype": "{doctype_4}"
# 	}
# ]


default_log_clearing_doctypes = {
	"Ecommerce Integration Log": 120,
	"AI Description Log": 90,
}
