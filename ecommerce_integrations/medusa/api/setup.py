"""Operator-facing setup endpoints for the Medusa Setting form.

Mirrors :mod:`ecommerce_integrations.shopware6.api.setup` for the Medusa
backend. Powers the health banner, safety mode button, webhook widget and
the 5-step setup wizard rendered in the Medusa Setting form. All endpoints
require a System Manager (see :func:`require_medusa_admin`).
"""

from __future__ import annotations

import json
import secrets
from typing import Any

import frappe
from frappe import _
from frappe.utils import get_url, now_datetime
from frappe.utils.data import add_to_date, get_datetime

from ecommerce_integrations.medusa.constants import MODULE_NAME, SETTING_DOCTYPE
from ecommerce_integrations.medusa.services.access import require_medusa_admin

WEBHOOK_ENDPOINT_PATH = (
    "/api/method/ecommerce_integrations.medusa.webhook_handler.handle_medusa_event"
)


def _get_setting():
    return frappe.get_doc(SETTING_DOCTYPE)


def _webhook_url() -> str:
    return f"{get_url().rstrip('/')}{WEBHOOK_ENDPOINT_PATH}"


def _count_logs(status: str, hours: int) -> int:
    cutoff = add_to_date(now_datetime(), hours=-hours)
    return frappe.db.count(
        "Ecommerce Integration Log",
        filters={
            "integration": MODULE_NAME,
            "status": status,
            "creation": [">", cutoff],
        },
    )


def _last_test_at(setting) -> str | None:
    for fname in ("last_test_connection", "last_test_at", "last_connection_test"):
        if hasattr(setting, fname):
            value = getattr(setting, fname)
            if value:
                return str(value)

    rows = frappe.get_all(
        "Ecommerce Integration Log",
        filters={
            "integration": MODULE_NAME,
            "method": ["like", "%test_connection%"],
        },
        fields=["creation", "status"],
        order_by="creation desc",
        limit=1,
    )
    if rows:
        return str(rows[0].creation)
    return None


def _last_webhook_at() -> str | None:
    rows = frappe.get_all(
        "Ecommerce Integration Log",
        filters={
            "integration": MODULE_NAME,
            "method": ["like", "%webhook%"],
        },
        fields=["creation"],
        order_by="creation desc",
        limit=1,
    )
    if rows:
        return str(rows[0].creation)
    return None


def _is_misconfigured(setting) -> bool:
    has_secret = bool(setting.webhook_secret)
    allow_unsigned = bool(getattr(setting, "allow_unsigned_webhooks", 0))
    if not has_secret and not allow_unsigned:
        return True
    return False


@frappe.whitelist()
def get_health() -> dict[str, Any]:
    """Return a plain-language health summary for the Medusa Setting form."""
    require_medusa_admin()
    setting = _get_setting()
    enabled = bool(setting.enable_medusa)

    if not enabled:
        return {
            "state": "grey",
            "message": _("Integration deaktiviert"),
            "detail": _("Schalte 'Enable Medusa' ein, um zu starten."),
            "last_test_at": _last_test_at(setting),
            "error_24h": 0,
            "success_24h": 0,
            "has_webhooks_recently": False,
        }

    error_24h = _count_logs("Error", 24)
    success_24h = _count_logs("Success", 24)
    last_test_at = _last_test_at(setting)
    last_webhook_at = _last_webhook_at()

    has_webhooks_recently = False
    if last_webhook_at:
        try:
            has_webhooks_recently = get_datetime(last_webhook_at) >= add_to_date(
                now_datetime(), hours=-24
            )
        except Exception:
            has_webhooks_recently = False

    tested_recently = False
    if last_test_at:
        try:
            tested_recently = get_datetime(last_test_at) >= add_to_date(
                now_datetime(), hours=-24
            )
        except Exception:
            tested_recently = False

    # A successful sync within the last 24h is strictly stronger evidence
    # the connection works than a one-off test_connection click — the
    # sync runs hundreds of API calls, the test does one. Don't nag the
    # operator about "untested" when the integration is visibly healthy.
    if not tested_recently and success_24h > 0:
        tested_recently = True

    misconfigured = _is_misconfigured(setting)
    recent_test_failure = frappe.db.count(
        "Ecommerce Integration Log",
        filters={
            "integration": MODULE_NAME,
            "method": ["like", "%test_connection%"],
            "status": "Error",
            "creation": [">", add_to_date(now_datetime(), hours=-24)],
        },
    )
    if misconfigured or recent_test_failure:
        return {
            "state": "red",
            "message": _("Konfigurationsproblem — Webhooks werden abgewiesen")
            if misconfigured
            else _("Verbindungstest fehlgeschlagen"),
            "detail": _(
                "Setze ein Webhook Secret oder erlaube ungesignierte Webhooks (nur für lokale Entwicklung)."
            )
            if misconfigured
            else _("Letzter Verbindungstest fehlgeschlagen. Bitte erneut testen."),
            "last_test_at": last_test_at,
            "error_24h": error_24h,
            "success_24h": success_24h,
            "has_webhooks_recently": has_webhooks_recently,
        }

    success_7d = _count_logs("Success", 24 * 7)

    if not tested_recently or error_24h > 0 or success_7d == 0:
        bits = []
        if not tested_recently:
            bits.append(_("Verbindung wurde länger nicht getestet"))
        if error_24h > 0:
            bits.append(_("{0} Fehler in 24h").format(error_24h))
        if success_7d == 0:
            bits.append(_("Keine erfolgreichen Syncs in 7 Tagen"))
        return {
            "state": "yellow",
            "message": _("Achtung — Aufmerksamkeit erforderlich"),
            "detail": " · ".join(bits),
            "last_test_at": last_test_at,
            "error_24h": error_24h,
            "success_24h": success_24h,
            "has_webhooks_recently": has_webhooks_recently,
        }

    return {
        "state": "green",
        "message": _("Verbunden — Bestellungen werden empfangen"),
        "detail": _("0 Fehler in 24h · {0} erfolgreiche Syncs").format(success_24h),
        "last_test_at": last_test_at,
        "error_24h": error_24h,
        "success_24h": success_24h,
        "has_webhooks_recently": has_webhooks_recently,
    }


@frappe.whitelist()
def get_webhook_info() -> dict[str, Any]:
    """Return webhook URL + reception status for the Webhook URL Widget."""
    require_medusa_admin()
    setting = _get_setting()
    last_received_at = _last_webhook_at()
    recent_count = frappe.db.count(
        "Ecommerce Integration Log",
        filters={
            "integration": MODULE_NAME,
            "method": ["like", "%webhook%"],
            "creation": [">", add_to_date(now_datetime(), days=-7)],
        },
    )
    return {
        "url": _webhook_url(),
        "secret_set": bool(setting.webhook_secret),
        "allow_unsigned": bool(getattr(setting, "allow_unsigned_webhooks", 0)),
        "last_received_at": last_received_at,
        "recent_count": recent_count,
    }


@frappe.whitelist()
def apply_safety_mode() -> dict[str, Any]:
    """Disable outbound pushes and pause Catalog Mirror runs for Medusa."""
    require_medusa_admin()
    setting = _get_setting()
    changes: list[str] = []

    if setting.upload_erpnext_items:
        setting.upload_erpnext_items = 0
        changes.append("upload_erpnext_items")
    if getattr(setting, "sync_item_on_update", 0):
        setting.sync_item_on_update = 0
        changes.append("sync_item_on_update")
    setting.save(ignore_permissions=False)

    mirrors_paused = 0
    if frappe.db.exists("DocType", "Ecommerce Catalog Mirror"):
        mirror_names = frappe.get_all(
            "Ecommerce Catalog Mirror",
            filters={"backend": "Medusa", "is_active": 1},
            pluck="name",
        )
        for name in mirror_names:
            frappe.db.set_value(
                "Ecommerce Catalog Mirror", name, "is_active", 0
            )
            mirrors_paused += 1
        if mirrors_paused:
            changes.append(f"catalog_mirrors_paused:{mirrors_paused}")

    frappe.db.commit()
    return {
        "success": True,
        "applied_changes": changes,
        "mirrors_paused": mirrors_paused,
    }


@frappe.whitelist()
def apply_setup_wizard(payload: str) -> dict[str, Any]:
    """Finalise the 5-step setup wizard for Medusa."""
    require_medusa_admin()

    data = json.loads(payload) if isinstance(payload, str) else payload
    setting = _get_setting()

    if data.get("medusa_url"):
        setting.medusa_url = data["medusa_url"]
    if data.get("api_key"):
        setting.api_key = data["api_key"]

    mode = (data.get("sync_mode") or "receive").lower()
    if mode == "full":
        setting.upload_erpnext_items = 1
        if hasattr(setting, "sync_item_on_update"):
            setting.sync_item_on_update = 1
    else:
        setting.upload_erpnext_items = 0
        if hasattr(setting, "sync_item_on_update"):
            setting.sync_item_on_update = 0

    if mode in ("receive", "manual") and frappe.db.exists(
        "DocType", "Ecommerce Catalog Mirror"
    ):
        for name in frappe.get_all(
            "Ecommerce Catalog Mirror",
            filters={"backend": "Medusa", "is_active": 1},
            pluck="name",
        ):
            frappe.db.set_value(
                "Ecommerce Catalog Mirror", name, "is_active", 0
            )

    if data.get("webhook_secret"):
        setting.webhook_secret = data["webhook_secret"]
    elif not setting.webhook_secret:
        setting.webhook_secret = secrets.token_urlsafe(32)

    setting.enable_medusa = 1
    setting.save(ignore_permissions=False)
    frappe.db.commit()

    return {
        "success": True,
        "mode": mode,
        "webhook_url": _webhook_url(),
        "webhook_secret_set": bool(setting.webhook_secret),
    }


@frappe.whitelist()
def fire_test_webhook() -> dict[str, Any]:
    """POST a synthetic event to our own Medusa webhook endpoint."""
    require_medusa_admin()
    setting = _get_setting()

    if not setting.enable_medusa:
        return {
            "success": False,
            "message": _("Medusa-Integration ist nicht aktiviert."),
        }

    payload = json.dumps(
        {
            "event": "test.ping",
            "data": {"id": "wizard-test", "source": "ecommerce_integrations_setup_wizard"},
        }
    )

    secret = setting.get_password("webhook_secret") if setting.webhook_secret else None
    headers = {"Content-Type": "application/json"}
    if secret:
        import hashlib
        import hmac

        signature = hmac.new(
            secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        headers["x-medusa-webhook-signature"] = signature

    import requests

    try:
        response = requests.post(
            _webhook_url(),
            data=payload,
            headers=headers,
            timeout=10,
        )
        ok = response.status_code < 400
        return {
            "success": ok,
            "status_code": response.status_code,
            "message": _("Test-Webhook erfolgreich empfangen.")
            if ok
            else _("Test-Webhook abgewiesen (HTTP {0}).").format(response.status_code),
            "url": _webhook_url(),
        }
    except Exception as e:
        return {
            "success": False,
            "message": _("Test-Webhook fehlgeschlagen: {0}").format(str(e)),
            "url": _webhook_url(),
        }


@frappe.whitelist()
def recent_logs(limit: int = 20) -> list[dict[str, Any]]:
    """Return recent Integration Log rows for the Details panel."""
    require_medusa_admin()
    try:
        limit = max(1, min(int(limit), 100))
    except Exception:
        limit = 20
    return frappe.get_all(
        "Ecommerce Integration Log",
        filters={"integration": MODULE_NAME},
        fields=["name", "creation", "method", "status", "message"],
        order_by="creation desc",
        limit=limit,
    )
