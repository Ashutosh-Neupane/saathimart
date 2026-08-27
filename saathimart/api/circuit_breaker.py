"""
Circuit breaker for vendor sync — prevents wasting resources pushing events
to a vendor whose site is down. After consecutive failures, stops trying
for a cooldown period, then allows a test request.

States: CLOSED (normal) → OPEN (blocking) → HALF_OPEN (testing) → CLOSED
"""
import frappe
from frappe.utils import now_datetime
from saathimart.api.redis_fallback import get_cache_fallback

# Configuration
FAILURE_THRESHOLD = 5      # consecutive failures before opening
COOLDOWN_SECONDS = 300     # 5 minute cooldown before half-open
SUCCESS_THRESHOLD = 2      # successes in half-open to close circuit


def record_delivery_failure(vendor_name):
    """Record a failed delivery attempt to a vendor."""
    cache = get_cache_fallback()
    key = f"sm_circuit:{vendor_name}"

    state = cache.get_value(key) or {"failures": 0, "state": "closed", "opened_at": None}
    state["failures"] = state.get("failures", 0) + 1
    state["last_failure"] = str(now_datetime())

    if state["failures"] >= FAILURE_THRESHOLD and state["state"] == "closed":
        state["state"] = "open"
        state["opened_at"] = str(now_datetime())
        _log_circuit_event(vendor_name, "opened", state["failures"])

    cache.set_value(key, state, expires_in_sec=3600)


def record_delivery_success(vendor_name):
    """Record a successful delivery to a vendor."""
    cache = get_cache_fallback()
    key = f"sm_circuit:{vendor_name}"

    state = cache.get_value(key) or {"failures": 0, "state": "closed"}
    if state["state"] == "half_open":
        state["successes"] = state.get("successes", 0) + 1
        if state["successes"] >= SUCCESS_THRESHOLD:
            state["state"] = "closed"
            state["failures"] = 0
            state["successes"] = 0
            _log_circuit_event(vendor_name, "closed")
    elif state["state"] == "closed":
        state["failures"] = 0  # reset on success

    cache.set_value(key, state, expires_in_sec=3600)


def should_attempt_delivery(vendor_name):
    """Check if we should attempt delivery to this vendor.

    Returns True if circuit is closed or half-open (test request allowed).
    Returns False if circuit is open (vendor is down, don't waste resources).
    """
    cache = get_cache_fallback()
    key = f"sm_circuit:{vendor_name}"
    state = cache.get_value(key)

    if not state or state["state"] == "closed":
        return True

    if state["state"] == "open":
        # Check if cooldown has elapsed
        opened_at = state.get("opened_at")
        if opened_at:
            from frappe.utils import time_diff_in_seconds
            elapsed = time_diff_in_seconds(now_datetime(), opened_at)
            if elapsed >= COOLDOWN_SECONDS:
                # Transition to half-open
                state["state"] = "half_open"
                state["successes"] = 0
                cache.set_value(key, state, expires_in_sec=3600)
                return True
        return False

    if state["state"] == "half_open":
        return True  # allow test request

    return True


def get_circuit_state(vendor_name):
    """Return current circuit state for a vendor."""
    cache = get_cache_fallback()
    state = cache.get_value(f"sm_circuit:{vendor_name}")
    if not state:
        return {"state": "closed", "failures": 0}
    return state


def get_all_circuit_states():
    """Return circuit states for all vendors (for dashboard)."""
    vendors = frappe.get_all("Vendor", fields=["name", "vendor_name"])
    result = []
    for v in vendors:
        state = get_circuit_state(v.name)
        result.append({
            "vendor": v.name,
            "vendor_name": v.vendor_name,
            "circuit_state": state.get("state", "closed"),
            "failures": state.get("failures", 0),
            "last_failure": state.get("last_failure"),
        })
    return result


def reset_circuit(vendor_name):
    """Manually reset a vendor's circuit (admin override)."""
    cache = get_cache_fallback()
    cache.delete_key(f"sm_circuit:{vendor_name}")
    _log_circuit_event(vendor_name, "manually_reset")


def _log_circuit_event(vendor_name, event, failures=0):
    try:
        frappe.log_error(
            title=f"Circuit Breaker — {event}",
            message=f"Vendor: {vendor_name}, Event: {event}, Failures: {failures}",
        )
    except Exception:
        pass
