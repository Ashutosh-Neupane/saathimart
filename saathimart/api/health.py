"""
Health check and monitoring endpoints.

Endpoints:
  health_check         — GET /api/method/saathimart.api.health.health_check
  metrics              — GET /api/method/saathimart.api.health.metrics
"""
import frappe
from frappe.utils import cint, now_datetime


@frappe.whitelist(allow_guest=True)
def health_check():
    """Basic health check for load balancers.

    Returns 200 OK if all dependencies are healthy.
    """
    status = {
        "status": "healthy",
        "timestamp": now_datetime().isoformat(),
        "checks": {}
    }

    # Check database
    try:
        frappe.db.sql("SELECT 1")
        status["checks"]["database"] = "ok"
    except Exception as e:
        status["checks"]["database"] = f"error: {str(e)}"
        status["status"] = "unhealthy"

    # Check Redis
    try:
        cache = frappe.cache()
        cache.ping()
        status["checks"]["redis"] = "ok"
    except Exception as e:
        status["checks"]["redis"] = f"error: {str(e)}"
        status["status"] = "unhealthy"

    # Check Frappe app availability
    try:
        frappe.get_doc("Settings", "Settings")
        status["checks"]["frappe"] = "ok"
    except Exception as e:
        status["checks"]["frappe"] = f"error: {str(e)}"
        status["status"] = "unhealthy"

    return status


@frappe.whitelist(allow_guest=True)
def metrics():
    """Prometheus-style metrics endpoint.

    Returns system metrics in a simple key-value format.
    """
    metrics = {
        "timestamp": now_datetime().isoformat(),
        "uptime_seconds": _get_uptime(),
        "database": {
            "connections": _get_db_connection_count(),
            "pool_size": frappe.conf.get("pool_size", 10),
        },
        "cache": {
            "hits": frappe.cache().get_value("metrics:cache_hits") or 0,
            "misses": frappe.cache().get_value("metrics:cache_misses") or 0,
        },
        "queue": {
            "short_pending": _get_queue_length("short"),
            "long_pending": _get_queue_length("long"),
        },
        "requests": {
            "total_24h": frappe.cache().get_value("metrics:requests_24h") or 0,
            "errors_24h": frappe.cache().get_value("metrics:errors_24h") or 0,
        },
    }
    return metrics


def _get_uptime():
    """Get server uptime in seconds."""
    import os
    if os.path.exists("/proc/uptime"):
        with open("/proc/uptime") as f:
            return cint(float(f.read().split()[0]))
    return 0


def _get_db_connection_count():
    """Get current database connection count."""
    try:
        result = frappe.db.sql("SHOW STATUS LIKE 'Threads_connected'", as_dict=True)
        return cint(result[0].value) if result else 0
    except Exception:
        return 0


def _get_queue_length(queue_name="short"):
    """Get number of jobs in queue."""
    try:
        from redis import Redis
        redis = Redis.from_url(frappe.conf.redis_queue)
        return redis.llen(f"queue:{queue_name}")
    except Exception:
        return 0
