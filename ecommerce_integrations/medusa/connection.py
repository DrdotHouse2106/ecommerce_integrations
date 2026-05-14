"""Medusa v2 API connection management with adaptive throttling."""

import contextlib
import functools
import threading
import time

import frappe
import requests

from ecommerce_integrations.medusa.constants import SETTING_DOCTYPE


class AdaptiveThrottle:
    """Adaptive rate limiter that slows down on errors and speeds up on success.

    State (current delay + consecutive-success counter) lives in Redis so
    every RQ worker on every node sees the same throttle. Without this, a
    429 storm only backs off one worker while the others happily keep
    hammering Medusa.

    The local ``threading.Lock`` is kept to serialise the small read-modify-
    write window inside a single process, and ``_last_request_ts`` stays
    process-local because the actual ``sleep`` is per-process by nature.
    """

    MIN_DELAY = 0.05      # Minimum delay between requests (50ms — low for local/LAN Medusa)
    MAX_DELAY = 10.0      # Cap at 10s between requests
    BACKOFF_FACTOR = 2.0  # Multiply delay on error
    RECOVERY_FACTOR = 0.7 # Multiply delay on success
    RECOVERY_THRESHOLD = 3 # Consecutive successes before reducing delay

    # Redis keys (shared across processes). TTL is generous — throttle state
    # naturally resets after a long idle period anyway.
    _DELAY_KEY = "medusa:throttle:delay"
    _SUCCESS_KEY = "medusa:throttle:successes"
    _STATE_TTL = 3600  # 1 hour

    def __init__(self):
        # ``_last_request_ts`` stays per-process — sleep timing is inherently
        # local. ``_lock`` only protects the local read-modify-write window.
        self._lock = threading.Lock()
        self._last_request_ts = 0.0

    # ── Redis-backed state ─────────────────────────────────────────

    def _get_delay(self) -> float:
        try:
            raw = frappe.cache.get_value(self._DELAY_KEY)
            if raw is None:
                return self.MIN_DELAY
            return float(raw)
        except Exception:
            return self.MIN_DELAY

    def _set_delay(self, value: float) -> None:
        try:
            frappe.cache.set_value(self._DELAY_KEY, float(value), expires_in_sec=self._STATE_TTL)
        except Exception:
            pass

    def _get_successes(self) -> int:
        try:
            raw = frappe.cache.get_value(self._SUCCESS_KEY)
            if raw is None:
                return 0
            return int(raw)
        except Exception:
            return 0

    def _set_successes(self, value: int) -> None:
        try:
            frappe.cache.set_value(self._SUCCESS_KEY, int(value), expires_in_sec=self._STATE_TTL)
        except Exception:
            pass

    # ── Public API (unchanged) ─────────────────────────────────────

    def wait(self):
        """Wait the current adaptive delay before making a request."""
        delay = self._get_delay()
        if delay > 0:
            time.sleep(delay)
        self._last_request_ts = time.time()

    def record_success(self):
        """Record a successful request — gradually reduce delay."""
        with self._lock:
            successes = self._get_successes() + 1
            delay = self._get_delay()
            if successes >= self.RECOVERY_THRESHOLD and delay > self.MIN_DELAY:
                new_delay = max(self.MIN_DELAY, delay * self.RECOVERY_FACTOR)
                self._set_delay(new_delay)
                self._set_successes(0)
            else:
                self._set_successes(successes)

    def record_error(self, is_overload=False):
        """Record a failed request — increase delay."""
        with self._lock:
            self._set_successes(0)
            delay = self._get_delay()
            if is_overload:
                # Server overloaded — significant backoff
                new_delay = min(self.MAX_DELAY, max(1.0, delay) * self.BACKOFF_FACTOR)
            else:
                # Other error — small bump
                new_delay = min(self.MAX_DELAY, delay + 0.5)
            self._set_delay(new_delay)

    @property
    def current_delay(self):
        return self._get_delay()

    def reset(self):
        with self._lock:
            self._set_delay(self.MIN_DELAY)
            self._set_successes(0)


# Singleton throttle instance. State lives in Redis, so every worker shares it.
_throttle = AdaptiveThrottle()


def get_throttle_status() -> dict:
    """Read-only diagnostics for the shared throttle (no side effects)."""
    return {
        "current_delay": _throttle._get_delay(),
        "consecutive_successes": _throttle._get_successes(),
        "min_delay": AdaptiveThrottle.MIN_DELAY,
        "max_delay": AdaptiveThrottle.MAX_DELAY,
    }

# Overload indicators
_OVERLOAD_STATUS_CODES = {429, 502, 503, 504}
_OVERLOAD_EXCEPTIONS = (requests.exceptions.ConnectionError, requests.exceptions.Timeout)


def get_throttle() -> AdaptiveThrottle:
    """Get the shared throttle instance (useful for monitoring)."""
    return _throttle


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
        "Authorization": f"Basic {api_key}",
    })
    session.timeout = 60

    return session, base_url


@contextlib.contextmanager
def optional_session(session=None, base_url=None):
    """Context manager that reuses an existing session or creates a new one.

    Usage:
        with optional_session(session, base_url) as (s, url):
            medusa_request(s, url, ...)
    """
    if session is not None:
        yield session, base_url
    else:
        session, base_url = get_medusa_session()
        try:
            yield session, base_url
        finally:
            session.close()


def temp_medusa_session(func):
    """Decorator that injects (session, base_url) after self (for methods) or
    as first two args (for plain functions).
    Handles retry with exponential backoff on overload errors."""
    import inspect
    _params = list(inspect.signature(func).parameters.keys())
    _is_method = _params and _params[0] == "self"

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        session, base_url = get_medusa_session()
        max_retries = 3
        delay = 2.0

        if _is_method:
            # Insert session, base_url after self
            call_args = (args[0], session, base_url) + args[1:]
        else:
            call_args = (session, base_url) + args

        try:
            for attempt in range(max_retries + 1):
                try:
                    return func(*call_args, **kwargs)
                except requests.exceptions.HTTPError as e:
                    if e.response is not None and e.response.status_code in _OVERLOAD_STATUS_CODES and attempt < max_retries:
                        _throttle.record_error(is_overload=True)
                        time.sleep(delay)
                        delay *= 2.0
                        continue
                    raise
                except _OVERLOAD_EXCEPTIONS:
                    if attempt < max_retries:
                        _throttle.record_error(is_overload=True)
                        time.sleep(delay)
                        delay *= 2.0
                        continue
                    raise
        finally:
            session.close()
    return wrapper


def medusa_request(session, base_url, method, path, **kwargs):
    """Make an API request to Medusa with adaptive throttling.

    Automatically waits based on recent error rates. Records success/failure
    to adjust future request pacing.
    """
    _throttle.wait()

    url = f"{base_url}{path}"
    try:
        response = session.request(method, url, **kwargs)
        response.raise_for_status()
        _throttle.record_success()
        if response.status_code == 204 or not response.content:
            return {}
        if "application/json" not in response.headers.get("content-type", ""):
            return {"_body": response.text}
        return response.json()
    except requests.exceptions.HTTPError as e:
        is_overload = e.response is not None and e.response.status_code in _OVERLOAD_STATUS_CODES
        if is_overload:
            _throttle.record_error(is_overload=True)
        # Don't throttle on 4xx client errors (validation, auth) — only on server overload
        raise
    except _OVERLOAD_EXCEPTIONS:
        _throttle.record_error(is_overload=True)
        raise


def medusa_request_all(session, base_url, path, data_key, page_size=100, **kwargs):
    """Fetch all items from a paginated Medusa endpoint.

    Medusa v2 has no 'no limit' option — limit=0 returns 0 results.
    This fetches pages of page_size until all items are retrieved.
    """
    all_items = []
    offset = 0
    params = kwargs.pop("params", {})

    while True:
        params["limit"] = page_size
        params["offset"] = offset
        result = medusa_request(session, base_url, "GET", path, params=params, **kwargs)
        items = result.get(data_key, [])
        all_items.extend(items)
        total = result.get("count", len(items))
        offset += page_size
        if offset >= total or not items:
            break

    return all_items


@frappe.whitelist()
def test_connection():
    """Test the Medusa API connection. Returns product count on success."""
    session, base_url = get_medusa_session()
    try:
        result = medusa_request(session, base_url, "GET", "/admin/products", params={"limit": 1})
        count = result.get("count", len(result.get("products", [])))
        return {"success": True, "message": "Connection successful", "product_count": count}
    except Exception as e:
        return {"success": False, "message": str(e)}
    finally:
        session.close()
