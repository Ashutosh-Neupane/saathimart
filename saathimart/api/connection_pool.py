"""
Connection pooling for outbound HTTP requests to vendors.

Instead of creating a new TCP connection for every webhook delivery,
reuse connections via urllib3's PoolManager. This reduces latency
by ~100ms per request (no TCP handshake + TLS negotiation) and
prevents port exhaustion under load.

Also handles:
  - Connection timeouts (don't wait forever for a vendor)
  - Read timeouts (vendor accepted connection but is slow)
  - Max connections per vendor (prevent one vendor from hogging all connections)
"""
import threading

import frappe
from frappe.utils import now_datetime

# Lazy-initialized pool manager
_pool = None
_pool_lock = threading.Lock()

# Configuration
CONNECT_TIMEOUT = 10      # seconds to establish connection
READ_TIMEOUT = 30         # seconds to read response
MAX_CONNECTIONS = 10      # max concurrent connections per vendor host
MAX_TOTAL = 50            # max total connections across all vendors

# Outbound throttle — the circuit breaker trips on *failure count*, which
# does nothing to stop a healthy-but-slow vendor from being hammered by a
# retry storm (a burst of newly-Queued events all landing in the same
# drain_event_queue pass). This caps raw request rate per vendor,
# independent of whether those requests are succeeding.
OUTBOUND_RATE_LIMIT = 30       # max deliveries per vendor per window
OUTBOUND_RATE_WINDOW = 60      # seconds


def should_throttle(vendor_name):
    """
    True if this vendor has already hit its outbound delivery rate this
    window — the caller should leave the event Queued and try again next
    pass, the same way a circuit-breaker-open or error-budget-exceeded
    vendor is skipped. Unlike those two, this isn't recording a failure —
    a throttled vendor may be perfectly healthy, just being delivered to
    faster than intended — so callers should NOT treat a throttle as a
    delivery error (no retry_count bump, no circuit-breaker failure).
    """
    key = f"sm_outbound_rate:{vendor_name}"
    try:
        cache = frappe.cache()
        current = cache.get_value(key)
        if current is None:
            cache.set_value(key, 1, expires_in_sec=OUTBOUND_RATE_WINDOW)
            return False
        if current >= OUTBOUND_RATE_LIMIT:
            return True
        cache.set_value(key, current + 1, expires_in_sec=OUTBOUND_RATE_WINDOW)
        return False
    except Exception:
        return False  # Redis down — fail open, same as the other checks


def get_pool():
    """Get or create a shared connection pool."""
    global _pool
    if _pool is not None:
        return _pool

    with _pool_lock:
        if _pool is not None:
            return _pool

        try:
            import urllib3
            _pool = urllib3.PoolManager(
                maxsize=MAX_TOTAL,
                timeout=urllib3.Timeout(
                    connect=CONNECT_TIMEOUT,
                    read=READ_TIMEOUT,
                ),
                retries=urllib3.Retry(
                    total=2,
                    backoff_factor=0.5,
                    status_forcelist=[502, 503, 504],
                ),
            )
        except ImportError:
            # urllib3 not available — fall through to requests default
            _pool = "fallback"

        return _pool


def pooled_request(method, url, headers=None, body=None, timeout=None):
    """Make an HTTP request using the connection pool.

    Returns (status_code, response_body, error).
    """
    pool = get_pool()

    if pool == "fallback":
        return _fallback_request(method, url, headers, body, timeout)

    try:
        import urllib3
        # urllib3 rejects a plain (connect, read) tuple — that's a
        # `requests` convention, not urllib3's. A bare `timeout` (int/float)
        # is fine as-is; only the connect/read pair needs wrapping.
        if timeout is None:
            request_timeout = urllib3.Timeout(connect=CONNECT_TIMEOUT, read=READ_TIMEOUT)
        elif isinstance(timeout, tuple):
            request_timeout = urllib3.Timeout(connect=timeout[0], read=timeout[1])
        else:
            request_timeout = timeout

        response = pool.request(
            method,
            url,
            headers=headers,
            body=body,
            timeout=request_timeout,
        )
        return response.status, response.data.decode("utf-8", errors="replace"), None
    except Exception as e:
        return 0, "", str(e)


def _fallback_request(method, url, headers=None, body=None, timeout=None):
    """Fallback using requests library if urllib3 not available."""
    import requests
    try:
        resp = requests.request(
            method,
            url,
            headers=headers,
            data=body,
            timeout=timeout or (CONNECT_TIMEOUT, READ_TIMEOUT),
        )
        return resp.status_code, resp.text, None
    except Exception as e:
        return 0, "", str(e)


def get_pool_stats():
    """Return pool statistics for monitoring."""
    pool = get_pool()
    if pool == "fallback":
        return {"pool": "fallback", "status": "using requests library"}

    try:
        return {
            "pool": "urllib3",
            "num_connections": pool.pool.num_connections if hasattr(pool.pool, 'num_connections') else 'unknown',
            "max_connections": MAX_TOTAL,
            "connect_timeout": CONNECT_TIMEOUT,
            "read_timeout": READ_TIMEOUT,
        }
    except Exception:
        return {"pool": "urllib3", "status": "ok"}
