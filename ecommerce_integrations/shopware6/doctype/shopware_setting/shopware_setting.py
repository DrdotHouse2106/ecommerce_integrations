"""Shopware Setting DocType Controller"""

import frappe
from frappe import _
from frappe.model.document import Document

from ecommerce_integrations.shopware6.services.access import require_shopware_admin
from ecommerce_integrations.shopware6.utils import get_logger


class ShopwareSetting(Document):
    def onload(self):
        """Load series options when form is opened."""
        self.load_series_options()
        self.load_bulk_sync_status()

    def validate(self):
        self.validate_credentials()

    def load_series_options(self):
        """Load naming series options into the form."""
        # Sales Order series
        so_series = frappe.get_meta("Sales Order").get_field("naming_series")
        if so_series:
            self.set("__onload", self.get("__onload") or frappe._dict())
            self.get("__onload").sales_order_series_options = so_series.options or ""

        # Delivery Note series
        dn_series = frappe.get_meta("Delivery Note").get_field("naming_series")
        if dn_series:
            self.get("__onload").delivery_note_series_options = dn_series.options or ""

        # Sales Invoice series
        si_series = frappe.get_meta("Sales Invoice").get_field("naming_series")
        if si_series:
            self.get("__onload").sales_invoice_series_options = si_series.options or ""

    def validate_credentials(self):
        """Validate that required credentials are provided based on grant type."""
        if not self.enable_shopware:
            return

        if self.grant_type == "resource_owner":
            if not self.client_id:
                frappe.throw(_("Client ID is required for Integration authentication"))
            if not self.client_secret:
                frappe.throw(_("Client Secret is required for Integration authentication"))
        elif self.grant_type == "user_credentials":
            if not self.api_username:
                frappe.throw(_("API Username is required for User Credentials authentication"))
            if not self.api_password:
                frappe.throw(_("API Password is required for User Credentials authentication"))

    def is_enabled(self) -> bool:
        """Check if Shopware integration is enabled."""
        return bool(self.enable_shopware)

    @frappe.whitelist()
    def test_connection(self):
        """Test the Shopware API connection."""
        require_shopware_admin()
        from ecommerce_integrations.shopware6.connection import test_connection

        result = test_connection()

        if result.get("success"):
            frappe.msgprint(
                _("Connection successful! Found {0} sales channel(s).").format(
                    result.get("sales_channels", 0)
                ),
                title=_("Success"),
                indicator="green",
            )
        else:
            frappe.msgprint(
                _("Connection failed: {0}").format(result.get("message")),
                title=_("Error"),
                indicator="red",
            )

        return result

    @frappe.whitelist()
    def fetch_shopware_warehouses(self):
        """Fetch warehouse/location data from Shopware."""
        require_shopware_admin()
        from ecommerce_integrations.shopware6.connection import get_shopware_client

        if not self.is_enabled():
            frappe.throw(_("Please enable Shopware integration first"))

        try:
            client = get_shopware_client()
            # In Shopware 6, stock is managed per "Stock Location" or directly on products
            # Try to fetch stock locations if the module is installed
            try:
                response = client.request_get("stock-location")
                locations = response.get("data", [])
            except Exception:
                # Stock locations may not be available in all Shopware installations
                locations = []

            if locations:
                frappe.msgprint(
                    _("Found {0} Shopware stock location(s)").format(len(locations)),
                    title=_("Success"),
                    indicator="green",
                )
            else:
                frappe.msgprint(
                    _("No stock locations found. Stock will be managed at product level."),
                    title=_("Info"),
                    indicator="blue",
                )

            return {"locations": locations}

        except Exception as e:
            get_logger().error("Error occurred", persist=False)
            frappe.throw(_("Failed to fetch Shopware warehouses: {0}").format(str(e)))

    def load_bulk_sync_status(self):
        """Load bulk sync queue status into the form."""
        try:
            from ecommerce_integrations.shopware6.bulk_sync import get_queue_status
            status = get_queue_status()
            self.set("__onload", self.get("__onload") or frappe._dict())
            self.get("__onload").bulk_sync_status = status

            # Generate HTML for the status field
            html = self._render_bulk_sync_status_html(status)
            self.bulk_sync_status_html = html
        except Exception:
            pass

    def _render_bulk_sync_status_html(self, status):
        """Render HTML for bulk sync status display."""
        bulk_mode = "Active" if status.get("bulk_mode_active") else "Inactive"
        bulk_mode_class = "red" if status.get("bulk_mode_active") else "green"
        product_count = status.get("product_queue_size", 0)
        properties_count = status.get("properties_queue_size", 0)

        return f"""
        <div style="padding: 10px; background: var(--bg-light-gray); border-radius: 4px;">
            <div style="display: flex; gap: 20px; flex-wrap: wrap; align-items: center;">
                <div><strong>Bulk Mode:</strong>
                    <span class="indicator-pill {bulk_mode_class}">{bulk_mode}</span></div>
                <div><strong>Products in Queue:</strong> {product_count}</div>
                <div><strong>Properties in Queue:</strong> {properties_count}</div>
            </div>
            <p class="text-muted small" style="margin: 8px 0 0;">
                Use Tools &gt; Process Queue Now / Clear Queue to manage the queue.
            </p>
        </div>
        """

    @frappe.whitelist()
    def get_bulk_sync_status(self):
        """Get current bulk sync queue status."""
        require_shopware_admin()
        from ecommerce_integrations.shopware6.bulk_sync import get_queue_status
        return get_queue_status()

    @frappe.whitelist()
    def force_process_bulk_sync(self):
        """Force process the bulk sync queue."""
        require_shopware_admin()
        from ecommerce_integrations.shopware6.bulk_sync import force_process_queue
        return force_process_queue()

    @frappe.whitelist()
    def clear_bulk_sync_queues(self):
        """Clear all bulk sync queues."""
        require_shopware_admin()
        from ecommerce_integrations.shopware6.bulk_sync import clear_all_queues
        return clear_all_queues()

    @frappe.whitelist()
    def fetch_sales_channels(self):
        """Fetch all Sales Channels from Shopware API and update the table."""
        require_shopware_admin()
        from ecommerce_integrations.shopware6.connection import get_shopware_client

        if not self.is_enabled():
            frappe.throw(_("Please enable Shopware integration first"))

        try:
            client = get_shopware_client()
            response = client.request_get("sales-channel")
            channels = response.get("data", [])

            # Remember existing channel configurations before refresh
            existing_config = {}
            for sc in self.sales_channels:
                existing_config[sc.sales_channel_id] = {
                    "is_default": sc.is_default,
                    "short_code": getattr(sc, 'short_code', ''),
                    "price_list": getattr(sc, 'price_list', None),
                    "price_adjustment_percent": getattr(sc, 'price_adjustment_percent', 0),
                    "shopware_rule_id": getattr(sc, 'shopware_rule_id', None),
                }

            # Clear existing and populate with fresh data
            self.sales_channels = []
            for channel in channels:
                channel_id = channel.get("id")
                channel_name = channel.get("name") or channel.get("translated", {}).get("name", "")

                # Restore existing config if available
                prev_config = existing_config.get(channel_id, {})

                self.append("sales_channels", {
                    "sales_channel_id": channel_id,
                    "sales_channel_name": channel_name,
                    "active": channel.get("active", True),
                    "access_key": channel.get("accessKey", ""),
                    "is_default": prev_config.get("is_default", 0),
                    "short_code": prev_config.get("short_code", ""),
                    "price_list": prev_config.get("price_list"),
                    "price_adjustment_percent": prev_config.get("price_adjustment_percent", 0),
                    "shopware_rule_id": prev_config.get("shopware_rule_id"),
                })

            # If no default was set, mark first active channel as default
            has_default = any(sc.is_default for sc in self.sales_channels)
            if not has_default and self.sales_channels:
                for sc in self.sales_channels:
                    if sc.active:
                        sc.is_default = 1
                        break

            # Create/update Shopware Rules for each active channel
            rules_created = self._ensure_channel_rules(client)

            self.save()
            frappe.msgprint(
                _("Found {0} Sales Channel(s). Created/verified {1} pricing rule(s).").format(
                    len(channels), rules_created
                ),
                title=_("Success"),
                indicator="green"
            )
            return {"channels": len(channels), "rules": rules_created}

        except Exception as e:
            get_logger().error("Error occurred", persist=False)
            frappe.throw(_("Failed to fetch Sales Channels: {0}").format(str(e)))

    def _ensure_channel_rules(self, client) -> int:
        """
        Ensure all active sales channels have corresponding Shopware Rules.

        Creates rules for channels that don't have one yet.

        Args:
            client: Shopware API client

        Returns:
            Number of rules created/verified
        """
        from ecommerce_integrations.shopware6.export.rule_handler import get_or_create_sales_channel_rule

        rules_count = 0
        for sc in self.sales_channels:
            if not sc.active:
                continue

            # Skip if rule already exists
            if sc.shopware_rule_id:
                rules_count += 1
                continue

            # Create rule for this channel
            rule_id = get_or_create_sales_channel_rule(
                client,
                sc.sales_channel_id,
                sc.sales_channel_name
            )

            if rule_id:
                sc.shopware_rule_id = rule_id
                rules_count += 1

        return rules_count

    def get_default_sales_channel_id(self):
        """Get the default Sales Channel ID."""
        for sc in self.sales_channels or []:
            if sc.is_default:
                return sc.sales_channel_id
        # Fallback to first active channel
        for sc in self.sales_channels or []:
            if sc.active:
                return sc.sales_channel_id
        return None

    def get_sales_channel_name(self, sales_channel_id):
        """Get Sales Channel name by ID."""
        for sc in self.sales_channels or []:
            if sc.sales_channel_id == sales_channel_id:
                return sc.sales_channel_name
        return None

    def get_all_active_sales_channel_ids(self):
        """Get list of all active Sales Channel IDs."""
        return [sc.sales_channel_id for sc in self.sales_channels or [] if sc.active]
