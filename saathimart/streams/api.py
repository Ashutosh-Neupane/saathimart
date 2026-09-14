"""
Stream Management API - Desk and external monitoring endpoints.

Provides endpoints for:
- Stream health dashboard
- Dead letter queue management
- Stream metrics and monitoring
"""
import frappe
import json
from frappe import _
from typing import Dict, Any, List
from frappe.utils import now_datetime


@frappe.whitelist()
def get_full_health_snapshot() -> Dict[str, Any]:
    """
    One call for the Stream Health desk page: mirror status, per-vendor
    stream/pending/dead-letter counts, and the DLQ contents. Everything is
    wrapped per-section so a Redis hiccup degrades one widget instead of
    erroring the whole page.
    """
    snapshot = {
        "checked_at": now_datetime().isoformat(),
        "mirror_enabled": bool(frappe.db.get_single_value(
            "SaathiMart Settings", "stream_mirror_enabled")),
        "mirror_note": _("Webhooks stay primary; streams are the durable second transport."),
        "vendors": [],
        "dead_letters": [],
    }

    # Discover vendor streams (hub:vendor:*:events) on the SAME Redis the
    # mirror publishes to — Settings.stream_redis_url when set, else this
    # site's cache Redis. Using a different client here would show an empty
    # dashboard while events flow happily on the other DB.
    try:
        from saathimart.streams.publisher import StreamPublisher
        redis_client = StreamPublisher("__probe__").redis
        keys = redis_client.scan_iter(match="hub:vendor:*:events", count=200)
        vendors = sorted({
            k.decode() if isinstance(k, bytes) else k
            for k in keys
        })
    except Exception:
        vendors = []

    from saathimart.streams.monitor import StreamMonitor
    from saathimart.streams.dead_letter import DeadLetterQueue

    for key in vendors:
        vendor_id = key.split(":")[2]
        entry = {"vendor": vendor_id, "length": 0, "pending": 0,
                 "dead_letters": 0, "consumers": []}
        try:
            m = StreamMonitor(vendor_id, redis=redis_client)
            health = m.check_health()
            entry["length"] = health.get("stream_length", 0)
            entry["pending"] = health.get("pending_count", 0)
        except Exception:
            pass
        try:
            dlq = DeadLetterQueue(vendor_id, redis=redis_client)
            entry["dead_letters"] = len(dlq.get_dead_letters(count=200))
        except Exception:
            pass
        snapshot["vendors"].append(entry)

    # DLQ contents across vendors (bounded). get_dead_letters returns
    # [{"msg_id", "data": {event fields...}}] — pull the event type out of
    # data for display.
    for v in snapshot["vendors"]:
        if not v["dead_letters"]:
            continue
        try:
            dlq = DeadLetterQueue(v["vendor"], redis=redis_client)
            for d in dlq.get_dead_letters(count=20):
                data = d.get("data") or {}
                event_type = data.get("event_type", "")
                try:
                    payload = json.loads(data.get("payload", "{}"))
                    event_type = event_type or payload.get("event_type", "")
                except Exception:
                    pass
                snapshot["dead_letters"].append({
                    "vendor": v["vendor"],
                    "msg_id": d.get("msg_id", ""),
                    "event_type": event_type,
                })
        except Exception:
            pass

    return snapshot


@frappe.whitelist()
def acknowledge_dead_letter(vendor_id: str, msg_id: str) -> Dict[str, Any]:
    """Discard a dead letter permanently (admin judged it unprocessable)."""
    from saathimart.streams.dead_letter import DeadLetterQueue
    DeadLetterQueue(vendor_id).acknowledge(msg_id)
    return {"ok": True}


@frappe.whitelist()
def get_stream_health(vendor_id: str = None) -> Dict[str, Any]:
    """
    Get health status of vendor streams.
    
    Args:
        vendor_id: Specific vendor (optional, returns all if not provided)
    
    Returns:
        Health check results
    """
    from saathimart.streams.monitor import StreamMonitor, check_all_streams
    
    if vendor_id:
        monitor = StreamMonitor(vendor_id)
        return monitor.check_health()
    
    return check_all_streams()


@frappe.whitelist()
def get_stream_metrics(vendor_id: str) -> Dict[str, Any]:
    """
    Get detailed metrics for a vendor's stream.
    
    Args:
        vendor_id: Vendor ID
    
    Returns:
        Stream metrics
    """
    from saathimart.streams.monitor import StreamMonitor
    
    monitor = StreamMonitor(vendor_id)
    return monitor.get_metrics()


@frappe.whitelist()
def get_stuck_messages(vendor_id: str, min_idle_seconds: int = 60) -> List[Dict[str, Any]]:
    """
    Get stuck messages from a vendor's stream.
    
    Args:
        vendor_id: Vendor ID
        min_idle_seconds: Minimum idle time
    
    Returns:
        List of stuck messages
    """
    from saathimart.streams.monitor import StreamMonitor
    
    monitor = StreamMonitor(vendor_id)
    return monitor.get_stuck_messages(min_idle_seconds)


@frappe.whitelist()
def get_dead_letters(vendor_id: str = None) -> Dict[str, Any]:
    """
    Get dead letters for a vendor or all vendors.
    
    Args:
        vendor_id: Specific vendor (optional)
    
    Returns:
        Dead letter summary
    """
    from saathimart.streams.dead_letter import DeadLetterQueue, get_dlq_summary
    
    if vendor_id:
        dlq = DeadLetterQueue(vendor_id)
        return {
            "vendor_id": vendor_id,
            "dead_letters": dlq.get_dead_letters(),
        }
    
    return get_dlq_summary()


@frappe.whitelist()
def retry_dead_letter(vendor_id: str, msg_id: str) -> Dict[str, Any]:
    """
    Retry a dead letter message.
    
    Args:
        vendor_id: Vendor ID
        msg_id: Dead letter message ID
    
    Returns:
        Success/failure status
    """
    from saathimart.streams.dead_letter import DeadLetterQueue
    
    dlq = DeadLetterQueue(vendor_id)
    
    # Get dead letter content
    dead_letters = dlq.get_dead_letters()
    msg_data = next((d["data"] for d in dead_letters if d["msg_id"] == msg_id), None)
    
    if not msg_data:
        return {"success": False, "error": "Message not found in DLQ"}
    
    success = dlq.retry_message(msg_id, msg_data)
    
    return {"success": success}


@frappe.whitelist()
def purge_stuck_messages(vendor_id: str, max_idle_hours: int = 24) -> Dict[str, Any]:
    """
    Purge very old stuck messages from a vendor's stream.
    
    WARNING: This removes messages permanently. Use with caution.
    
    Args:
        vendor_id: Vendor ID
        max_idle_hours: Messages idle > this many hours will be purged
    
    Returns:
        Number of messages purged
    """
    # Check permissions
    if "System Manager" not in frappe.get_roles():
        frappe.throw(_("Only System Managers can purge messages"), frappe.PermissionError)
    
    from saathimart_vendor.streams.worker import purge_stuck_messages as do_purge
    
    count = do_purge(vendor_id, max_idle_hours)
    
    return {
        "vendor_id": vendor_id,
        "purged_count": count,
    }


@frappe.whitelist()
def check_dlq_and_move() -> Dict[str, Any]:
    """
    Check all DLQs and move eligible messages.
    
    Returns:
        Summary of moved messages
    """
    from saathimart.streams.dead_letter import check_all_dead_letter_queues
    
    return check_all_dead_letter_queues()


@frappe.whitelist()
def get_stream_dashboard_data() -> Dict[str, Any]:
    """
    Get all data for stream health dashboard.
    
    Returns:
        Complete dashboard data
    """
    from saathimart.streams.monitor import check_all_streams
    from saathimart.streams.dead_letter import get_dlq_summary
    
    stream_health = check_all_streams()
    dlq_summary = get_dlq_summary()
    
    return {
        "streams": stream_health,
        "dead_letters": dlq_summary,
        "timestamp": frappe.utils.now_datetime().isoformat(),
    }
