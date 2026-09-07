"""
Stream Management API - Desk and external monitoring endpoints.

Provides endpoints for:
- Stream health dashboard
- Dead letter queue management
- Stream metrics and monitoring
"""
import frappe
from frappe import _
from typing import Dict, Any, List


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
