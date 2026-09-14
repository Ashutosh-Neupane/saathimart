"""
Dead Letter Queue - Handle messages that failed processing after max retries.

Messages that consistently fail are moved to a dead letter stream for
manual inspection and handling. This prevents them from blocking the
main stream indefinitely.
"""
import frappe
import json
from frappe import _
from frappe.utils import now_datetime
from typing import Dict, Any, List
from saathimart.streams.publisher import StreamPublisher


class DeadLetterQueue:
    """
    Manages failed messages that exceeded retry limits.
    
    When a message fails after MAX_RETRIES attempts, it's moved to
    a dead letter stream for manual inspection. This prevents stuck
    messages from blocking the main processing queue.
    
    Usage:
        dlq = DeadLetterQueue("VENDOR001")
        
        # Check for dead letters
        dead = dlq.get_dead_letters()
        
        # Retry a dead letter
        dlq.retry_message(msg_id, payload)
        
        # Acknowledge (remove) a dead letter
        dlq.acknowledge(msg_id)
    """
    
    DEAD_LETTER_STREAM_PREFIX = "hub:vendor"
    DEAD_LETTER_SUFFIX = ":dead-letter"
    MAX_RETRIES = 5  # Move to DLQ after 5 failed attempts
    
    def __init__(self, vendor_id: str, redis=None):
        self.vendor_id = vendor_id
        self.main_stream = f"hub:vendor:{vendor_id}:events"
        self.dead_letter_stream = f"{self.DEAD_LETTER_STREAM_PREFIX}:{vendor_id}{self.DEAD_LETTER_SUFFIX}"
        self._redis = redis
    
    @property
    def redis(self):
        if self._redis is None:
            # Same stream Redis the mirror publishes to — Settings override,
            # else this site's cache Redis.
            url = frappe.db.get_single_value("SaathiMart Settings", "stream_redis_url")
            if url:
                import redis as redis_mod
                self._redis = redis_mod.Redis.from_url(
                    url, decode_responses=False, socket_timeout=5, socket_connect_timeout=5
                )
            else:
                self._redis = frappe.cache()
        return self._redis
    
    def check_and_move_to_dlq(self) -> int:
        """
        Check for messages that exceeded retry limit and move to DLQ.
        
        Returns:
            Number of messages moved to dead letter queue
        """
        try:
            # Get pending messages
            pending = self.redis.execute_command(
                "XPENDING", self.main_stream, "vendor-workers",
                "-", "+", 100
            )
            
            if not pending:
                return 0
            
            moved_count = 0
            
            for msg in pending:
                msg_id, consumer, idle_time, deliveries = msg
                
                if isinstance(msg_id, bytes):
                    msg_id = msg_id.decode()
                
                # Check if exceeded max retries
                if deliveries >= self.MAX_RETRIES:
                    # Get message content
                    msg_data = self._get_message_content(msg_id)
                    
                    if msg_data:
                        # Add to dead letter stream
                        self._add_to_dlq(msg_id, msg_data, deliveries)
                        
                        # Acknowledge from main stream (remove)
                        self.redis.execute_command(
                            "XACK", self.main_stream, "vendor-workers", msg_id
                        )
                        
                        # Log to database for audit
                        self._log_dead_letter(msg_id, msg_data, deliveries)
                        
                        moved_count += 1
            
            return moved_count
            
        except Exception as e:
            frappe.log_error(frappe.get_traceback(), f"Failed to check DLQ: {str(e)}")
            return 0
    
    def get_dead_letters(self, count: int = 50) -> List[Dict[str, Any]]:
        """
        Get messages from dead letter queue.
        
        Args:
            count: Maximum number of messages to fetch
        
        Returns:
            List of dead letter messages
        """
        try:
            messages = self.redis.execute_command(
                "XREAD", "COUNT", str(count),
                "STREAMS", self.dead_letter_stream, "0"
            )
            
            if not messages:
                return []
            
            dead_letters = []
            for stream_data in messages:
                for msg_id, fields in stream_data[1]:
                    msg_dict = {}
                    for i in range(0, len(fields), 2):
                        key = fields[i].decode() if isinstance(fields[i], bytes) else fields[i]
                        value = fields[i + 1].decode() if isinstance(fields[i + 1], bytes) else fields[i + 1]
                        msg_dict[key] = value
                    
                    dead_letters.append({
                        "msg_id": msg_id.decode() if isinstance(msg_id, bytes) else msg_id,
                        "data": msg_dict,
                    })
            
            return dead_letters
            
        except Exception as e:
            frappe.log_error(frappe.get_traceback(), f"Failed to get dead letters: {str(e)}")
            return []
    
    def retry_message(self, msg_id: str, original_payload: Dict[str, Any]) -> bool:
        """
        Retry a dead letter message by re-publishing to main stream.
        
        Args:
            msg_id: Original message ID
            original_payload: Original event payload
        
        Returns:
            True if successfully retried
        """
        try:
            # Publish to main stream
            publisher = StreamPublisher(self.vendor_id)
            new_msg_id = publisher.publish(
                original_payload.get("event_type"),
                json.loads(original_payload.get("payload", "{}"))
            )
            
            # Acknowledge from DLQ
            self.acknowledge(msg_id)
            
            frappe.logger().info(
                f"Retried dead letter {msg_id} as {new_msg_id}"
            )
            
            return True
            
        except Exception as e:
            frappe.log_error(frappe.get_traceback(), f"Failed to retry dead letter {msg_id}: {str(e)}")
            return False
    
    def acknowledge(self, msg_id: str):
        """
        Remove message from dead letter queue (acknowledge as handled).
        
        Args:
            msg_id: Message ID in DLQ
        """
        try:
            # Create a temporary consumer group for acknowledgment
            # (DLQ doesn't have consumer groups by default)
            self.redis.execute_command(
                "XDEL", self.dead_letter_stream, msg_id
            )
            
            # Log acknowledgment
            frappe.logger().info(f"Acknowledged dead letter {msg_id}")
            
        except Exception as e:
            frappe.log_error(frappe.get_traceback(), f"Failed to acknowledge DLQ message {msg_id}: {str(e)}")
    
    def _get_message_content(self, msg_id: str) -> Dict[str, Any]:
        """Get message content from main stream."""
        try:
            result = self.redis.execute_command(
                "XRANGE", self.main_stream, msg_id, msg_id
            )
            
            if not result:
                return {}
            
            msg_id, fields = result[0]
            msg_dict = {}
            for i in range(0, len(fields), 2):
                key = fields[i].decode() if isinstance(fields[i], bytes) else fields[i]
                value = fields[i + 1].decode() if isinstance(fields[i + 1], bytes) else fields[i + 1]
                msg_dict[key] = value
            
            return msg_dict
            
        except Exception:
            return {}
    
    def _add_to_dlq(self, msg_id: str, msg_data: Dict[str, Any], deliveries: int):
        """Add message to dead letter stream."""
        dlq_entry = {
            "original_msg_id": msg_id,
            "event_type": msg_data.get("event_type", "unknown"),
            "payload": msg_data.get("payload", "{}"),
            "vendor_id": self.vendor_id,
            "failed_at": now_datetime().isoformat(),
            "delivery_attempts": str(deliveries),
            "error_reason": "Exceeded max retries",
        }
        
        self.redis.execute_command(
            "XADD", self.dead_letter_stream,
            "*",
            *sum([[k, v] for k, v in dlq_entry.items()], [])
        )
    
    def _log_dead_letter(self, msg_id: str, msg_data: Dict[str, Any], deliveries: int):
        """Log dead letter to database for audit trail."""
        try:
            log = frappe.new_doc("Dead Letter Log")
            log.vendor_id = self.vendor_id
            log.original_msg_id = msg_id
            log.event_type = msg_data.get("event_type", "unknown")
            log.payload = msg_data.get("payload", "{}")
            log.delivery_attempts = deliveries
            log.failed_at = now_datetime()
            log.insert(ignore_permissions=True)
        except Exception as e:
            # Don't fail the DLQ process if logging fails
            frappe.log_error(frappe.get_traceback(), f"Failed to log dead letter: {str(e)}")


def check_all_dead_letter_queues() -> Dict[str, int]:
    """
    Check all vendor DLQs and move eligible messages.
    
    Returns:
        Summary of messages moved per vendor
    """
    vendors = frappe.get_all("Vendor", filters={"status": "Active"}, pluck="name")
    
    results = {}
    
    for vendor_id in vendors:
        dlq = DeadLetterQueue(vendor_id)
        moved = dlq.check_and_move_to_dlq()
        
        if moved > 0:
            results[vendor_id] = moved
            frappe.logger().warning(
                f"Moved {moved} messages to DLQ for vendor {vendor_id}"
            )
    
    return results


def get_dlq_summary() -> Dict[str, Any]:
    """
    Get summary of all dead letter queues.
    
    Returns:
        Dict with DLQ counts per vendor
    """
    vendors = frappe.get_all("Vendor", filters={"status": "Active"}, pluck="name")
    
    summary = {
        "total_dead_letters": 0,
        "vendors": [],
    }
    
    for vendor_id in vendors:
        dlq = DeadLetterQueue(vendor_id)
        dead_letters = dlq.get_dead_letters(count=100)
        
        count = len(dead_letters)
        
        if count > 0:
            summary["vendors"].append({
                "vendor_id": vendor_id,
                "count": count,
                "oldest": dead_letters[0] if dead_letters else None,
            })
            summary["total_dead_letters"] += count
    
    return summary
