"""
Clock skew tolerance — handles time differences between hub and vendor servers.

Problem: Hub and vendor servers may have clocks that differ by seconds or
even minutes. If the HMAC timestamp window is too tight, legitimate requests
get rejected. If too wide, replay attacks have more opportunity.

Solution: Adaptive clock skew detection + configurable tolerance window.

The hub periodically pings each vendor's server time and calculates the
average skew. This skew is added to the tolerance window so HMAC verification
works even with 30+ seconds of clock difference.
"""
import frappe
from frappe.utils import now_datetime, time_diff_in_seconds

DEFAULT_SKEW_TOLERANCE = 300  # 5 minutes — accounts for NTP drift
MAX_ACCEPTABLE_SKEW = 600     # 10 minutes — beyond this, alert admin


def get_vendor_clock_skew(vendor_name):
    """Get the measured clock skew for a vendor (in seconds).

    Positive = vendor clock is ahead of hub.
    Negative = vendor clock is behind hub.
    """
    cache_key = f"sm_clock_skew:{vendor_name}"
    try:
        cache = frappe.cache()
        skew = cache.get_value(cache_key)
        if skew is not None:
            return float(skew)
    except Exception:
        pass

    # Fall back to DB
    try:
        skew = frappe.db.get_value("Vendor", vendor_name, "clock_skew_seconds")
        return float(skew or 0)
    except Exception:
        return 0


def set_vendor_clock_skew(vendor_name, skew_seconds):
    """Record the measured clock skew for a vendor."""
    cache_key = f"sm_clock_skew:{vendor_name}"
    try:
        cache = frappe.cache()
        cache.set_value(cache_key, skew_seconds, expires_in_sec=3600)
    except Exception:
        pass

    # Persist to DB
    try:
        frappe.db.set_value("Vendor", vendor_name, "clock_skew_seconds", skew_seconds)
        frappe.db.commit()
    except Exception:
        pass

    # Alert if skew is too large
    if abs(skew_seconds) > MAX_ACCEPTABLE_SKEW:
        frappe.log_error(
            title=f"Clock Skew Alert — {vendor_name}",
            message=f"Vendor {vendor_name} clock is {skew_seconds:.1f}s "
                    f"{'ahead' if skew_seconds > 0 else 'behind'} hub. "
                    f"Max acceptable: {MAX_ACCEPTABLE_SKEW}s",
        )


def is_timestamp_valid(timestamp_str, vendor_name=None, tolerance=None):
    """Check if a timestamp is within the acceptable window, accounting for skew.

    Args:
        timestamp_str: ISO timestamp from the request
        vendor_name: if provided, applies that vendor's measured skew
        tolerance: override default tolerance (seconds)
    """
    from frappe.utils import get_datetime

    if tolerance is None:
        tolerance = DEFAULT_SKEW_TOLERANCE

    try:
        request_time = get_datetime(timestamp_str)
    except Exception:
        return False

    now = now_datetime()
    diff = abs(time_diff_in_seconds(now, request_time))

    # Apply vendor-specific skew tolerance
    if vendor_name:
        skew = abs(get_vendor_clock_skew(vendor_name))
        tolerance = max(tolerance, skew + 60)  # skew + 1 minute buffer

    return diff <= tolerance


def measure_clock_skew(vendor_name, vendor_server_time_str):
    """Calculate clock skew from a vendor's response.

    Call this when the hub receives a response with X-SM-Timestamp header.
    The skew = vendor_time - hub_time (in seconds).
    """
    from frappe.utils import get_datetime

    try:
        vendor_time = get_datetime(vendor_server_time_str)
        hub_time = now_datetime()
        skew = time_diff_in_seconds(vendor_time, hub_time)
        set_vendor_clock_skew(vendor_name, skew)
        return skew
    except Exception:
        return 0


def get_all_skews():
    """Get clock skews for all vendors (for dashboard)."""
    vendors = frappe.get_all("Vendor", fields=["name", "vendor_name"])
    result = []
    for v in vendors:
        skew = get_vendor_clock_skew(v.name)
        result.append({
            "vendor": v.name,
            "vendor_name": v.vendor_name,
            "skew_seconds": skew,
            "is_acceptable": abs(skew) <= MAX_ACCEPTABLE_SKEW,
        })
    return result
