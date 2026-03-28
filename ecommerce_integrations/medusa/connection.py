"""Medusa v2 API connection management."""

import functools
import time

import frappe
import requests

from ecommerce_integrations.medusa.constants import SETTING_DOCTYPE


def get_medusa_session() -> tuple:
    """Return (requests.Session, base_url) configured with Medusa API Key auth."""
    setting = frappe.get_cached_doc(SETTING_DOCTYPE)
    if not setting.medusa_url or not setting.api_key:
        frappe.throw("Medusa URL and API Key must be configured in Medusa Setting")

    base_url = setting.medusa_url.rstrip("/")
    api_key = setting.get_password("api_key")

    session = requests.Session()
    session.headers.update({
        "Content-Type": "application/json",
        "Accept": "application/json",
        "x-medusa-access-token": api_key,
    })
    session.timeout = 60

    return session, base_url


def temp_medusa_session(func):
    """Decorator that injects (session, base_url) as first two args.
    Handles retry with exponential backoff on 502/503/504."""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        session, base_url = get_medusa_session()
        max_retries = 3
        delay = 2.0

        for attempt in range(max_retries + 1):
            try:
                return func(session, base_url, *args, **kwargs)
            except requests.exceptions.HTTPError as e:
                if e.response is not None and e.response.status_code in (502, 503, 504) and attempt < max_retries:
                    time.sleep(delay)
                    delay *= 2.0
                    continue
                raise
            finally:
                session.close()
    return wrapper


def medusa_request(session, base_url, method, path, **kwargs):
    """Make an API request to Medusa and return parsed JSON."""
    url = f"{base_url}{path}"
    response = session.request(method, url, **kwargs)
    response.raise_for_status()
    if response.status_code == 204:
        return {}
    return response.json()


@frappe.whitelist()
def test_connection():
    """Test the Medusa API connection."""
    try:
        session, base_url = get_medusa_session()
        response = session.get(f"{base_url}/admin/products", params={"limit": 1})
        response.raise_for_status()
        session.close()
        return {"success": True, "message": "Connection successful"}
    except Exception as e:
        return {"success": False, "message": str(e)}
