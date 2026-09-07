"""
Stream Publisher - Hub-side event publishing using Redis Streams.

Uses Frappe's built-in Redis client (frappe.cache()) to execute
Redis Streams commands. No additional packages required.

Redis Streams is available in Redis 5.0+ and Frappe uses Redis 6+
in production, so all XADD, XREADGROUP, XACK commands work.

Integration with frappe.enqueue():
    - Publishers enqueue background tasks (fast API response)
    - Background workers execute XADD to Redis Stream
    - Vendor consumers read via XREADGROUP with consumer groups
"""
import frappe
import json
from frappe import _
from frappe.utils import now_datetime
from typing import Dict, Any, List, Optional
import time


class StreamPublisher:
    """
    Publishes events to vendor-specific Redis Streams.
    
    Each vendor has their own stream for isolation and security.
    Events are persisted in Redis until consumers acknowledge them.
    
    Usage:
        publisher = StreamPublisher("VENDOR001")
        msg_id = publisher.publish("order.new", {"order_id": "ORD-001", ...})
    """
    
    STREAM_PREFIX = "hub:vendor"
    MAX_STREAM_LENGTH = 10000  # Trim old messages (~10k events per vendor)
    
    def __init__(self, vendor_id: str):
        self.vendor_id = vendor_id
        self.stream_name = f"{self.STREAM_PREFIX}:{vendor_id}:events"
        self._redis = None
    
    @property
    def redis(self):
        """Lazy-load Redis client from Frappe's cache."""
        if self._redis is None:
            self._redis = frappe.cache()
        return self._redis
    
    def publish(self, event_type: str, payload: Dict[str, Any], 
                event_id: Optional[str] = None) -> str:
        """
        Publish a single event to the vendor's stream.
        
        Args:
            event_type: Event type (e.g., "order.new", "stock.update")
            payload: Event data (will be JSON-serialized)
            event_id: Optional unique event ID (auto-generated if not provided)
        
        Returns:
            Redis Stream message ID (timestamp-based, e.g., "1526569495631-0")
        
        Example:
            >>> publisher.publish("order.new", {"order_id": "ORD-001"})
            "1526569495631-0"
        """
        event = {
            "event_type": event_type,
            "event_id": event_id or f"{int(time.time() * 1000)}-{frappe.generate_hash(length=8)}",
            "timestamp": now_datetime().isoformat(),
            "vendor_id": self.vendor_id,
            "payload": json.dumps(payload, default=str),
        }
        
        # XADD with MAXLEN for automatic trimming
        # "~" means approximate trimming (more efficient)
        msg_id = self.redis.execute_command(
            "XADD", self.stream_name,
            "MAXLEN", "~", str(self.MAX_STREAM_LENGTH),
            "*",
            *sum([[k, v] for k, v in event.items()], [])
        )
        
        return msg_id.decode() if isinstance(msg_id, bytes) else msg_id
    
    def publish_batch(self, events: List[tuple]) -> List[str]:
        """
        Publish multiple events in a single pipeline (efficient for bulk operations).
        
        Args:
            events: List of (event_type, payload) tuples
        
        Returns:
            List of message IDs
        
        Example:
            >>> publisher.publish_batch([
            ...     ("order.new", {"order_id": "ORD-001"}),
            ...     ("stock.update", {"product": "PROD-001", "qty": 100}),
            ... ])
            ["1526569495631-0", "1526569495631-1"]
        """
        pipe = self.redis.pipeline()
        
        for event_type, payload in events:
            event = {
                "event_type": event_type,
                "event_id": f"{int(time.time() * 1000)}-{frappe.generate_hash(length=8)}",
                "timestamp": now_datetime().isoformat(),
                "vendor_id": self.vendor_id,
                "payload": json.dumps(payload, default=str),
            }
            
            pipe.execute_command(
                "XADD", self.stream_name,
                "MAXLEN", "~", str(self.MAX_STREAM_LENGTH),
                "*",
                *sum([[k, v] for k, v in event.items()], [])
            )
        
        msg_ids = pipe.execute()
        return [mid.decode() if isinstance(mid, bytes) else mid for mid in msg_ids]
    
    def get_stream_info(self) -> Dict[str, Any]:
        """
        Get stream metadata (length, groups, first/last message).
        
        Returns:
            Dict with stream information
        
        Example:
            >>> publisher.get_stream_info()
            {
                "length": 5234,
                "groups": 1,
                "first_entry": "1526569495631-0",
                "last_entry": "1526569498945-3"
            }
        """
        try:
            info = self.redis.execute_command("XINFO", "STREAM", self.stream_name)
            
            # Parse XINFO response (alternating key-value pairs)
            result = {}
            for i in range(0, len(info), 2):
                key = info[i].decode() if isinstance(info[i], bytes) else info[i]
                value = info[i + 1]
                if isinstance(value, bytes):
                    value = value.decode()
                result[key] = value
            
            return result
        except Exception as e:
            if "no such key" in str(e).lower():
                return {"length": 0, "groups": 0, "error": "Stream does not exist"}
            raise
    
    def get_pending_count(self, group_name: str = "vendor-workers") -> int:
        """
        Get count of pending (unacknowledged) messages.
        
        Args:
            group_name: Consumer group name
        
        Returns:
            Number of pending messages
        """
        try:
            pending = self.redis.execute_command(
                "XPENDING", self.stream_name, group_name
            )
            # XPENDING returns: [count, min_id, max_id, consumers]
            return pending[0] if pending else 0
        except Exception:
            return 0


def publish_to_vendor(vendor_id: str, event_type: str, payload: Dict[str, Any]) -> str:
    """
    Convenience function to publish a single event to a vendor.
    
    This is the primary API for hub-side code to send events.
    
    Args:
        vendor_id: Target vendor ID
        event_type: Event type (e.g., "order.new")
        payload: Event data
    
    Returns:
        Message ID
    
    Example:
        >>> from saathimart.streams import publish_to_vendor
        >>> publish_to_vendor("VENDOR001", "order.new", {"order_id": "ORD-001"})
        "1526569495631-0"
    """
    publisher = StreamPublisher(vendor_id)
    return publisher.publish(event_type, payload)


def publish_order_created(order) -> str:
    """
    Publish order.new event when order is created.
    
    Called from Order.after_insert() via hooks.py.
    Uses frappe.enqueue for background processing.
    """
    # Enqueue background task to publish to Redis Stream
    frappe.enqueue(
        "saathimart.streams.tasks.publish_order_created_task",
        order_name=order.name,
        queue="short",
        timeout=60,
        enqueue_after_commit=True,
        job_id=f"publish-order-{order.name}",
    )
    
    return f"queued-{order.name}"


def publish_payment_received(order_id: str, amount: float, gateway: str) -> str:
    """
    Publish payment.received event when payment is confirmed.
    
    Uses frappe.enqueue for background processing.
    """
    frappe.enqueue(
        "saathimart.streams.tasks.publish_payment_received_task",
        order_name=order_id,
        amount=amount,
        gateway=gateway,
        queue="short",
        timeout=60,
        enqueue_after_commit=True,
        job_id=f"publish-payment-{order_id}",
    )
    
    return f"queued-payment-{order_id}"
