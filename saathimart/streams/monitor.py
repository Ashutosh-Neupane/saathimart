"""
Stream Monitoring - Track stream health, lag, and stuck messages.

Provides real-time monitoring of Redis Streams for operations team.
Can be integrated with Frappe's desk or external monitoring tools.
"""
import frappe
import json
from frappe import _
from frappe.utils import now_datetime, add_to_date
from typing import Dict, Any, List
from saathimart.streams.publisher import StreamPublisher


class StreamMonitor:
    """
    Monitor Redis Stream health and performance.
    
    Usage:
        monitor = StreamMonitor("VENDOR001")
        health = monitor.check_health()
        if health["status"] != "healthy":
            send_alert(health)
    """
    
    STREAM_PREFIX = "hub:vendor"
    
    # Thresholds for alerting
    MAX_PENDING_THRESHOLD = 100  # Alert if > 100 pending messages
    MAX_IDLE_TIME_MINUTES = 10   # Alert if oldest pending > 10 minutes
    MAX_STREAM_LENGTH = 10000    # Alert if stream length > 10k
    
    def __init__(self, vendor_id: str):
        self.vendor_id = vendor_id
        self.stream_name = f"{self.STREAM_PREFIX}:{vendor_id}:events"
        self._redis = None
    
    @property
    def redis(self):
        if self._redis is None:
            self._redis = frappe.cache()
        return self._redis
    
    def check_health(self) -> Dict[str, Any]:
        """
        Comprehensive health check for the stream.
        
        Returns:
            {
                "status": "healthy" | "warning" | "critical",
                "stream_length": 1234,
                "pending_count": 5,
                "oldest_pending_age_seconds": 120,
                "consumer_groups": 1,
                "active_consumers": 2,
                "alerts": ["High pending count: 150"]
            }
        """
        alerts = []
        status = "healthy"
        
        # Get stream info
        stream_info = self._get_stream_info()
        pending_info = self._get_pending_info()
        
        # Check stream length
        stream_length = stream_info.get("length", 0)
        if stream_length > self.MAX_STREAM_LENGTH:
            alerts.append(f"High stream length: {stream_length}")
            status = "warning"
        
        # Check pending messages
        pending_count = pending_info.get("count", 0)
        if pending_count > self.MAX_PENDING_THRESHOLD:
            alerts.append(f"High pending count: {pending_count}")
            status = "warning" if status != "critical" else status
        
        # Check oldest pending message age
        oldest_pending_age = self._get_oldest_pending_age_seconds()
        if oldest_pending_age and oldest_pending_age > self.MAX_IDLE_TIME_MINUTES * 60:
            alerts.append(f"Stuck message detected: {oldest_pending_age}s old")
            status = "critical"
        
        return {
            "status": status,
            "vendor_id": self.vendor_id,
            "stream_name": self.stream_name,
            "stream_length": stream_length,
            "pending_count": pending_count,
            "oldest_pending_age_seconds": oldest_pending_age,
            "consumer_groups": stream_info.get("groups", 0),
            "active_consumers": len(pending_info.get("consumers", [])),
            "alerts": alerts,
            "checked_at": now_datetime().isoformat(),
        }
    
    def get_metrics(self) -> Dict[str, Any]:
        """
        Get detailed metrics for dashboards.
        
        Returns:
            Dict with all stream metrics
        """
        stream_info = self._get_stream_info()
        pending_info = self._get_pending_info()
        
        return {
            "vendor_id": self.vendor_id,
            "stream": {
                "name": self.stream_name,
                "length": stream_info.get("length", 0),
                "first_entry": stream_info.get("first_entry"),
                "last_entry": stream_info.get("last_entry"),
                "groups": stream_info.get("groups", 0),
            },
            "pending": {
                "count": pending_info.get("count", 0),
                "min_id": pending_info.get("min_id"),
                "max_id": pending_info.get("max_id"),
                "consumers": pending_info.get("consumers", []),
            },
            "timestamp": now_datetime().isoformat(),
        }
    
    def get_stuck_messages(self, min_idle_seconds: int = 60) -> List[Dict[str, Any]]:
        """
        Get messages that have been idle for too long.
        
        Args:
            min_idle_seconds: Minimum idle time
        
        Returns:
            List of stuck message details
        """
        try:
            pending = self.redis.execute_command(
                "XPENDING", self.stream_name, "vendor-workers",
                "-", "+", 100
            )
            
            if not pending:
                return []
            
            stuck = []
            for msg in pending:
                msg_id, consumer, idle_time, deliveries = msg
                
                if isinstance(msg_id, bytes):
                    msg_id = msg_id.decode()
                if isinstance(consumer, bytes):
                    consumer = consumer.decode()
                
                idle_seconds = idle_time / 1000
                
                if idle_seconds >= min_idle_seconds:
                    stuck.append({
                        "msg_id": msg_id,
                        "consumer": consumer,
                        "idle_seconds": idle_seconds,
                        "deliveries": deliveries,
                        "status": "stuck" if idle_seconds > 300 else "slow",
                    })
            
            return stuck
            
        except Exception as e:
            frappe.log_error(frappe.get_traceback(), f"Failed to get stuck messages: {str(e)}")
            return []
    
    def _get_stream_info(self) -> Dict[str, Any]:
        """Get XINFO STREAM output."""
        try:
            info = self.redis.execute_command("XINFO", "STREAM", self.stream_name)
            
            result = {}
            for i in range(0, len(info), 2):
                key = info[i].decode() if isinstance(info[i], bytes) else info[i]
                value = info[i + 1]
                if isinstance(value, bytes):
                    value = value.decode()
                result[key] = value
            
            return result
        except Exception:
            return {"length": 0, "groups": 0}
    
    def _get_pending_info(self) -> Dict[str, Any]:
        """Get XPENDING summary."""
        try:
            pending = self.redis.execute_command(
                "XPENDING", self.stream_name, "vendor-workers"
            )
            
            if not pending:
                return {"count": 0}
            
            count, min_id, max_id, consumers = pending
            
            return {
                "count": count,
                "min_id": min_id.decode() if isinstance(min_id, bytes) else min_id,
                "max_id": max_id.decode() if isinstance(max_id, bytes) else max_id,
                "consumers": [
                    {
                        "consumer": c.decode() if isinstance(c, bytes) else c,
                        "count": cnt
                    }
                    for c, cnt in (consumers or [])
                ]
            }
        except Exception:
            return {"count": 0}
    
    def _get_oldest_pending_age_seconds(self) -> int:
        """Get age of oldest pending message in seconds."""
        try:
            pending = self.redis.execute_command(
                "XPENDING", self.stream_name, "vendor-workers",
                "-", "+", 1
            )
            
            if not pending:
                return 0
            
            msg_id, consumer, idle_time, deliveries = pending[0]
            return idle_time // 1000  # Convert ms to seconds
        except Exception:
            return 0


def check_all_streams() -> Dict[str, Any]:
    """
    Check health of all vendor streams.
    
    Returns:
        Summary of all stream health checks
    """
    vendors = frappe.get_all("Vendor", filters={"status": "Active"}, pluck="name")
    
    results = {
        "healthy": 0,
        "warning": 0,
        "critical": 0,
        "total": len(vendors),
        "vendors": [],
    }
    
    for vendor_id in vendors:
        monitor = StreamMonitor(vendor_id)
        health = monitor.check_health()
        
        results["vendors"].append(health)
        
        if health["status"] == "healthy":
            results["healthy"] += 1
        elif health["status"] == "warning":
            results["warning"] += 1
        else:
            results["critical"] += 1
    
    return results


def send_stream_alert(health: Dict[str, Any]):
    """
    Send alert for stream issues (email, Slack, etc.).
    
    Args:
        health: Health check result
    """
    vendor_id = health.get("vendor_id", "Unknown")
    alerts = health.get("alerts", [])
    
    subject = f"[ALERT] Stream issues for vendor {vendor_id}"
    
    message = f"""
    <h2>Stream Health Alert</h2>
    <p><strong>Vendor:</strong> {vendor_id}</p>
    <p><strong>Status:</strong> {health.get('status')}</p>
    <p><strong>Pending Messages:</strong> {health.get('pending_count')}</p>
    <p><strong>Oldest Pending Age:</strong> {health.get('oldest_pending_age_seconds')}s</p>
    
    <h3>Alerts:</h3>
    <ul>
    {''.join(f'<li>{alert}</li>' for alert in alerts)}
    </ul>
    
    <p><a href="/app/stream-monitor/{vendor_id}">View Details</a></p>
    """
    
    # Send to administrators
    recipients = frappe.get_all(
        "User",
        filters={"role": "System Manager"},
        pluck="email"
    )
    
    if recipients:
        frappe.sendmail(
            recipients=recipients,
            subject=subject,
            message=message,
            reference_doctype="Vendor",
            reference_name=vendor_id,
            queue=True
        )


def send_stream_health_digest():
    """
    Send daily health digest for all streams.
    
    Called by scheduler (daily).
    Only sends email if there are issues.
    """
    from saathimart.streams.dead_letter import get_dlq_summary
    
    results = check_all_streams()
    dlq_summary = get_dlq_summary()
    
    # Only send if there are warnings or critical issues
    if results["warning"] == 0 and results["critical"] == 0 and dlq_summary["total_dead_letters"] == 0:
        return
    
    subject = f"[DAILY] Stream Health Digest - {results['warning']} warnings, {results['critical']} critical"
    
    message = f"""
    <h2>Daily Stream Health Digest</h2>
    
    <h3>Summary</h3>
    <ul>
        <li><strong>Healthy:</strong> {results['healthy']}</li>
        <li><strong>Warnings:</strong> {results['warning']}</li>
        <li><strong>Critical:</strong> {results['critical']}</li>
        <li><strong>Dead Letters:</strong> {dlq_summary['total_dead_letters']}</li>
    </ul>
    
    """
    
    # Add vendor details
    if results["critical"] > 0:
        message += "<h3>Critical Issues</h3><ul>"
        for vendor in results["vendors"]:
            if vendor["status"] == "critical":
                message += f"""
                <li>
                    <strong>{vendor['vendor_id']}</strong>: 
                    {vendor.get('pending_count', 0)} pending, 
                    oldest {vendor.get('oldest_pending_age_seconds', 0)}s old
                </li>
                """
        message += "</ul>"
    
    if dlq_summary["total_dead_letters"] > 0:
        message += "<h3>Dead Letter Queues</h3><ul>"
        for vendor in dlq_summary["vendors"]:
            message += f"""
            <li><strong>{vendor['vendor_id']}</strong>: {vendor['count']} dead letters</li>
            """
        message += "</ul>"
    
    message += '<p><a href="/app/stream-health-dashboard">View Dashboard</a></p>'
    
    recipients = frappe.get_all(
        "User",
        filters={"role": "System Manager"},
        pluck="email"
    )
    
    if recipients:
        frappe.sendmail(
            recipients=recipients,
            subject=subject,
            message=message,
            queue=True
        )
