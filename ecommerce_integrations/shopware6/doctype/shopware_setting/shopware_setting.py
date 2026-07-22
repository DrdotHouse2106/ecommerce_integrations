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
                frappe.throw(_("Client-ID ist für die Integrations-Authentifizierung erforderlich"))
            if not self.client_secret:
                frappe.throw(_("Client-Secret ist für die Integrations-Authentifizierung erforderlich"))
        elif self.grant_type == "user_credentials":
            if not self.api_username:
                frappe.throw(_("API-Benutzername ist für die Benutzer-Authentifizierung erforderlich"))
            if not self.api_password:
                frappe.throw(_("API-Passwort ist für die Benutzer-Authentifizierung erforderlich"))

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
                _("Verbindung erfolgreich! {0} Verkaufskanal/-kanäle gefunden.").format(
                    result.get("sales_channels", 0)
                ),
                title=_("Success"),
                indicator="green",
            )
        else:
            frappe.msgprint(
                _("Verbindung fehlgeschlagen: {0}").format(result.get("message")),
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
            frappe.throw(_("Bitte zuerst die Shopware-Integration aktivieren"))

        try:
            client = get_shopware_client()
            # In Shopware 6, stock is managed per "Stock Location" or directly on products
            # Try to fetch stock locations if the module is installed
            try:
                response = client.request_get("stock-location")
                locations = response.data or []
            except Exception:
                # Stock locations may not be available in all Shopware installations
                locations = []

            if locations:
                frappe.msgprint(
                    _("{0} Shopware-Lagerstandort(e) gefunden").format(len(locations)),
                    title=_("Success"),
                    indicator="green",
                )
            else:
                frappe.msgprint(
                    _("Keine Lagerstandorte gefunden. Bestand wird auf Produktebene verwaltet."),
                    title=_("Info"),
                    indicator="blue",
                )

            return {"locations": locations}

        except Exception as e:
            get_logger().error("Error occurred", persist=False)
            frappe.throw(_("Shopware-Lager konnten nicht abgerufen werden: {0}").format(str(e)))

    @frappe.whitelist()
    def fetch_sales_channels(self):
        """Fetch all Sales Channels from Shopware API and update the table."""
        require_shopware_admin()
        from ecommerce_integrations.shopware6.connection import get_shopware_client

        if not self.is_enabled():
            frappe.throw(_("Bitte zuerst die Shopware-Integration aktivieren"))

        try:
            client = get_shopware_client()
            response = client.request_get("sales-channel")
            channels = response.data or []

            # Fetch domain map keyed by sales-channel UUID. Surfacing the
            # storefront URL in the Catalog Mirror picker makes "which
            # channel is which" obvious without round-tripping to the
            # Shopware admin. We swallow errors here — a missing
            # domain field is a UX regression, not a sync blocker.
            domain_map = self._fetch_sales_channel_domains(client)

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
                    "domain_url": domain_map.get(channel_id, ""),
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
                _("{0} Verkaufskanal/-kanäle gefunden. {1} Preisregel(n) erstellt/geprüft.").format(
                    len(channels), rules_created
                ),
                title=_("Success"),
                indicator="green"
            )
            return {"channels": len(channels), "rules": rules_created}

        except Exception as e:
            get_logger().error("Error occurred", persist=False)
            frappe.throw(_("Verkaufskanäle konnten nicht abgerufen werden: {0}").format(str(e)))

    def _fetch_sales_channel_domains(self, client) -> dict[str, str]:
        """Return ``{sales_channel_id: domain_url}`` for the primary domain
        of each sales channel.

        Shopware lets one sales channel carry many domain rows (one per
        language). We keep the first one we see — it's good enough for a
        human-friendly picker label. If the API call fails we swallow
        the error and return an empty map so the surrounding refresh
        still succeeds; the picker just falls back to "Name (UUID)".
        """
        try:
            response = client.request_get("sales-channel-domain")
        except Exception:
            get_logger().error(
                "Failed to fetch sales-channel-domain — picker labels will "
                "fall back to UUIDs",
                persist=False,
            )
            return {}
        out: dict[str, str] = {}
        for row in (response.data if response else None) or []:
            attrs = row.get("attributes") or row
            sc_id = attrs.get("salesChannelId") or row.get("salesChannelId")
            url = (attrs.get("url") or row.get("url") or "").strip()
            if sc_id and url and sc_id not in out:
                out[sc_id] = url
        return out

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
