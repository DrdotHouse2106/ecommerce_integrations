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

    Thread-safe. Shared across all requests in the same process.
    """

    MIN_DELAY = 0.5       # Minimum delay between requests (backpressure floor)
    MAX_DELAY = 15.0      # Cap at 15s between requests
    BACKOFF_FACTOR = 2.0  # Multiply delay on error
    RECOVERY_FACTOR = 0.7 # Multiply delay on success
    RECOVERY_THRESHOLD = 3 # Consecutive successes before reducing delay

    def __init__(self):
        self._delay = self.MIN_DELAY
        self._consecutive_successes = 0
        self._lock = threading.Lock()

    def wait(self):
        """Wait the current adaptive delay before making a request."""
        with self._lock:
            delay = self._delay
        if delay > 0:
            time.sleep(delay)

    def record_success(self):
        """Record a successful request — gradually reduce delay."""
        with self._lock:
            self._consecutive_successes += 1
            if self._consecutive_successes >= self.RECOVERY_THRESHOLD and self._delay > self.MIN_DELAY:
                self._delay = max(self.MIN_DELAY, self._delay * self.RECOVERY_FACTOR)
                self._consecutive_successes = 0

    def record_error(self, is_overload=False):
        """Record a failed request — increase delay."""
        with self._lock:
            self._consecutive_successes = 0
            if is_overload:
                # Server overloaded — significant backoff
                self._delay = min(self.MAX_DELAY, max(1.0, self._delay) * self.BACKOFF_FACTOR)
            else:
                # Other error — small bump
                self._delay = min(self.MAX_DELAY, self._delay + 0.5)

    @property
    def current_delay(self):
        with self._lock:
            return self._delay

    def reset(self):
        with self._lock:
            self._delay = self.MIN_DELAY
            self._consecutive_successes = 0


# Singleton throttle shared across all Medusa API calls in this process
_throttle = AdaptiveThrottle()

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
    """Decorator that injects (session, base_url) as first two args.
    Handles retry with exponential backoff on overload errors."""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        session, base_url = get_medusa_session()
        max_retries = 3
        delay = 2.0

        try:
            for attempt in range(max_retries + 1):
                try:
                    return func(session, base_url, *args, **kwargs)
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
        if response.status_code == 204:
            return {}
        return response.json()
    except requests.exceptions.HTTPError as e:
        is_overload = e.response is not None and e.response.status_code in _OVERLOAD_STATUS_CODES
        _throttle.record_error(is_overload=is_overload)
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
