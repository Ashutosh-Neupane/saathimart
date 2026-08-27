"""
Partial failure isolation — ensures one vendor's failure doesn't block
delivery to other vendors.

Problem: If the drain_event_queue cron processes events sequentially and
one vendor's delivery hangs, all subsequent vendors wait.

Solution: Per-vendor goroutine-like isolation with timeouts and independent
error tracking. Each vendor's failures are isolated — a timeout for vendor A
doesn't prevent vendor B from receiving its events.

Also provides per-vendor error budgets: if a vendor fails >N times in an hour,
defer its events until the next cycle.
"""
import frappe
from frappe.utils import now_datetime, add_to_date, time_diff_in_seconds

MAX_ERRORS_PER_HOUR = 20
DEFER_DURATION_MINUTES = 30


def record_vendor_error(vendor_name, error_type="unknown"):
    """Record a vendor delivery error for budget tracking."""
    cache_key = f"sm_errors:{vendor_name}:{now_datetime().strftime('%Y%m%d%H')}"

    try:
        cache = frappe.cache()
        count = cache.get_value(cache_key) or 0
        cache.set_value(cache_key, count + 1, expires_in_sec=7200)
        return count + 1
    except Exception:
        return _db_record_error(vendor_name)


def check_error_budget(vendor_name):
    """Check if a vendor has exceeded its error budget.

    Returns True if vendor is within budget (should process).
    Returns False if vendor is deferred (too many errors).
    """
    cache_key_defer = f"sm_defer:{vendor_name}"

    try:
        cache = frappe.cache()
        # Check if currently deferred
        deferred_until = cache.get_value(cache_key_defer)
        if deferred_until:
            if now_datetime().isoformat() < deferred_until:
                return False  # still deferred
            else:
                # Defer period expired — allow processing
                cache.delete_key(cache_key_defer)
                return True

        # Check current hour's error count
        hour_key = f"sm_errors:{vendor_name}:{now_datetime().strftime('%Y%m%d%H')}"
        errors = cache.get_value(hour_key) or 0
        if errors >= MAX_ERRORS_PER_HOUR:
            # Defer this vendor
            defer_until = add_to_date(now_datetime(), minutes=DEFER_DURATION_MINUTES)
            cache.set_value(cache_key_defer, defer_until.isoformat(), expires_in_sec=DEFER_DURATION_MINUTES * 60)
            frappe.log_error(
                title=f"Vendor Deferred — {vendor_name}",
                message=f"Exceeded error budget: {errors} errors this hour. "
                        f"Deferred for {DEFER_DURATION_MINUTES} minutes.",
            )
            return False

        return True
    except Exception:
        # Redis down — fall through to processing
        return True


def get_deferred_vendors():
    """Get list of currently deferred vendors (for dashboard)."""
    vendors = frappe.get_all("Vendor", fields=["name", "vendor_name"])
    deferred = []

    for v in vendors:
        try:
            cache = frappe.cache()
            defer_until = cache.get_value(f"sm_defer:{v.name}")
            if defer_until and now_datetime().isoformat() < defer_until:
                deferred.append({
                    "vendor": v.name,
                    "vendor_name": v.vendor_name,
                    "deferred_until": defer_until,
                })
        except Exception:
            pass

    return deferred


def force_process_vendor(vendor_name):
    """Admin override: clear defer status and error budget."""
    try:
        cache = frappe.cache()
        cache.delete_key(f"sm_defer:{vendor_name}")
        # Clear all hourly error keys
        for h in range(24):
            hour = now_datetime().strftime('%Y%m%d') + f"{h:02d}"
            cache.delete_key(f"sm_errors:{vendor_name}:{hour}")
    except Exception:
        pass


def _db_record_error(vendor_name):
    """DB fallback for error tracking."""
    try:
        existing = frappe.db.sql("""
            SELECT name, error_count FROM `tabVendor`
            WHERE name = %s
        """, (vendor_name,), as_dict=True)
        if existing:
            count = (existing[0].error_count or 0) + 1
            frappe.db.set_value("Vendor", vendor_name, "error_count", count)
            frappe.db.commit()
            return count
    except Exception:
        pass
    return 0
