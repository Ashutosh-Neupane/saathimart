"""
Health check endpoints — lightweight probes for monitoring hub ↔ vendor
connectivity, Redis availability, MariaDB responsiveness, and overall
system health.

Endpoints:
  - health_check():          Public, returns basic alive status
  - deep_health_check():     Authenticated, returns full system status
  - vendor_health(vendor):   Check specific vendor connectivity
  - sync_health_dashboard(): Admin dashboard data
"""
import json
import time

import frappe
from frappe import _


@frappe.whitelist()
def health_check():
    """Public health check — returns 200 if hub is alive.

    Used by load balancers, Docker health checks, and monitoring tools.
    """
    return {"status": "ok", "timestamp": str(time.time())}


@frappe.whitelist()
def deep_health_check():
    """Full system health check — authenticated.

    Returns status of all subsystems: MariaDB, Redis, event queue, etc.
    """
    checks = {}

    # MariaDB
    try:
        start = time.time()
        frappe.db.sql("SELECT 1")
        checks["mariadb"] = {"status": "ok", "latency_ms": round((time.time() - start) * 1000, 1)}
    except Exception as e:
        checks["mariadb"] = {"status": "error", "error": str(e)}

    # Redis
    try:
        start = time.time()
        cache = frappe.cache()
        cache.set_value("_health_ping", 1, expires_in_sec=5)
        result = cache.get_value("_health_ping")
        latency = round((time.time() - start) * 1000, 1)
        checks["redis"] = {"status": "ok" if result == 1 else "degraded", "latency_ms": latency}
    except Exception as e:
        checks["redis"] = {"status": "error", "error": str(e)}

    # Event queue
    try:
        queued = frappe.db.count("Webhook Event", {"status": "Queued"})
        dead = frappe.db.count("Webhook Event", {"status": "Dead"})
        checks["event_queue"] = {
            "status": "ok" if dead < 10 else "warning",
            "queued": queued,
            "dead": dead,
        }
    except Exception as e:
        checks["event_queue"] = {"status": "error", "error": str(e)}

    # Vendors
    try:
        vendor_count = frappe.db.count("Vendor", {"status": "Active"})
        checks["vendors"] = {"status": "ok", "active_count": vendor_count}
    except Exception as e:
        checks["vendors"] = {"status": "error", "error": str(e)}

    # Overall status
    statuses = [c.get("status") for c in checks.values()]
    if "error" in statuses:
        overall = "degraded"
    elif "warning" in statuses:
        overall = "warning"
    else:
        overall = "ok"

    return {
        "status": overall,
        "timestamp": str(time.time()),
        "checks": checks,
    }


@frappe.whitelist()
def vendor_health(vendor_name=None):
    """
    Check health of a specific vendor's sync connection.

    Three separate systems each track their own idea of "is this vendor
    OK" — circuit breaker (consecutive delivery failures), partial_failure
    (hourly error budget / defer status), and clock_sync (measured clock
    skew widening its HMAC timestamp tolerance). Before this, an admin had
    to know to check all three separately (three different API calls, three
    different cache-key namespaces) to get the full picture of one vendor.
    This returns all three together so "unhealthy" always means something
    concrete and visible right here, not a value hidden in a sibling
    endpoint.
    """
    if not vendor_name:
        frappe.throw(_("Vendor name required"))

    result = {
        "vendor": vendor_name,
        "status": "unknown",
        "last_event": None,
        "circuit_state": "closed",
        "recent_errors": 0,
        "error_budget": {"within_budget": True, "deferred": False},
        "clock_skew_seconds": 0,
    }

    # Last successful delivery
    last_sent = frappe.db.sql("""
        SELECT name, event_type, creation
        FROM `tabWebhook Event`
        WHERE target_vendor = %s AND status = 'Sent'
        ORDER BY creation DESC LIMIT 1
    """, (vendor_name,), as_dict=True)

    if last_sent:
        result["last_event"] = {
            "name": last_sent[0].name,
            "type": last_sent[0].event_type,
            "time": str(last_sent[0].creation),
        }

    # Circuit state
    try:
        from saathimart.api.circuit_breaker import get_circuit_state
        result["circuit_state"] = get_circuit_state(vendor_name)
    except Exception:
        pass

    # Error budget / defer status (api/partial_failure.py)
    try:
        from saathimart.api.partial_failure import check_error_budget
        within_budget = check_error_budget(vendor_name)
        result["error_budget"] = {"within_budget": within_budget, "deferred": not within_budget}
    except Exception:
        pass

    # Measured clock skew (api/clock_sync.py) — informational; a large
    # value means this vendor's HMAC timestamp tolerance has widened well
    # past the 5-minute default, worth a human glancing at even though it
    # doesn't by itself make the vendor "unhealthy".
    try:
        from saathimart.api.clock_sync import get_vendor_clock_skew, MAX_ACCEPTABLE_SKEW
        skew = get_vendor_clock_skew(vendor_name)
        result["clock_skew_seconds"] = skew
        result["clock_skew_acceptable"] = abs(skew) <= MAX_ACCEPTABLE_SKEW
    except Exception:
        pass

    # Recent errors
    try:
        result["recent_errors"] = frappe.db.count("Webhook Event", {
            "target_vendor": vendor_name,
            "status": "Dead",
            "creation": (">=", frappe.utils.add_to_date(None, hours=-24)),
        })
    except Exception:
        pass

    result["status"] = (
        "healthy"
        if result["circuit_state"].get("state") == "closed"
        and not result["error_budget"]["deferred"]
        and result["recent_errors"] == 0
        else "unhealthy"
    )

    return result


@frappe.whitelist()
def sync_health_dashboard():
    """Admin dashboard data — aggregated sync health across all vendors."""
    vendors = frappe.get_all("Vendor", fields=["name", "vendor_name", "status"])

    vendor_healths = []
    for v in vendors:
        try:
            h = vendor_health(v.name)
            vendor_healths.append(h)
        except Exception:
            vendor_healths.append({
                "vendor": v.name,
                "status": "error",
            })

    # Queue stats
    queue_stats = frappe.db.sql("""
        SELECT status, priority, COUNT(*) as cnt
        FROM `tabWebhook Event`
        GROUP BY status, priority
    """, as_dict=True)

    # Dead letter breakdown
    dead_by_vendor = frappe.db.sql("""
        SELECT target_vendor, COUNT(*) as cnt
        FROM `tabWebhook Event`
        WHERE status = 'Dead'
        GROUP BY target_vendor
        ORDER BY cnt DESC
    """, as_dict=True)

    healthy = sum(1 for h in vendor_healths if h.get("status") == "healthy")
    unhealthy = sum(1 for h in vendor_healths if h.get("status") != "healthy")

    return {
        "total_vendors": len(vendors),
        "healthy_vendors": healthy,
        "unhealthy_vendors": unhealthy,
        "vendor_details": vendor_healths,
        "queue_stats": queue_stats,
        "dead_letters_by_vendor": dead_by_vendor,
        "timestamp": str(time.time()),
    }
