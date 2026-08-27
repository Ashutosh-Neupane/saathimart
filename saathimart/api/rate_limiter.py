"""
Auth failure rate limiter — blocks brute-force attacks on webhook endpoints.

Tracks failed authentication attempts per IP using Redis. After
MAX_FAILURES failures within the WINDOW, the IP is blocked for BLOCK_DURATION.
"""
import frappe
from frappe import _
from saathimart.api.redis_fallback import get_cache_fallback

# Configuration
MAX_FAILURES = 10          # failures before block
WINDOW_SECONDS = 300       # 5 minute window
BLOCK_DURATION = 900       # 15 minute block


def check_rate_limit(key, max_failures=None, window=None):
    """Check if a key (typically IP) has exceeded the failure threshold.

    Returns True if request should be allowed, False if blocked.
    """
    max_failures = max_failures or MAX_FAILURES
    window = window or WINDOW_SECONDS

    cache = get_cache_fallback()
    block_key = f"sm_auth_block:{key}"
    count_key = f"sm_auth_count:{key}"

    # Check if currently blocked
    if cache.get_value(block_key):
        return False

    # Check failure count
    count = cache.get_value(count_key) or 0
    if count >= max_failures:
        # Block the IP
        cache.set_value(block_key, 1, expires_in_sec=BLOCK_DURATION)
        cache.delete_key(count_key)
        _log_rate_limit_event(key, "blocked")
        return False

    return True


def record_failure(key, window=None):
    """Record an auth failure for the given key. Increments counter."""
    window = window or WINDOW_SECONDS
    cache = get_cache_fallback()
    count_key = f"sm_auth_count:{key}"

    count = cache.get_value(count_key) or 0
    cache.set_value(count_key, count + 1, expires_in_sec=window)

    if count + 1 >= MAX_FAILURES:
        _log_rate_limit_event(key, "threshold_reached")


def clear_failures(key):
    """Clear failure count for a key (e.g. after successful auth)."""
    cache = get_cache_fallback()
    cache.delete_key(f"sm_auth_count:{key}")
    cache.delete_key(f"sm_auth_block:{key}")


def is_blocked(key):
    """Check if a key is currently blocked."""
    cache = get_cache_fallback()
    return bool(cache.get_value(f"sm_auth_block:{key}"))


def get_failure_count(key):
    """Get current failure count for a key."""
    cache = get_cache_fallback()
    return cache.get_value(f"sm_auth_count:{key}") or 0


def _log_rate_limit_event(key, event_type):
    """Log rate limit events for security monitoring."""
    try:
        frappe.log_error(
            title=f"Auth Rate Limit — {event_type}",
            message=f"Key: {key}, Event: {event_type}, "
                    f"Max failures: {MAX_FAILURES}, Window: {WINDOW_SECONDS}s, "
                    f"Block duration: {BLOCK_DURATION}s",
        )
    except Exception:
        pass
