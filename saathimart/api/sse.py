"""
Server-Sent Events (SSE) for real-time order status updates.

Mobile apps and Next.js frontend can subscribe to order status changes
without polling. The SSE connection stays open and receives events
whenever the order status changes.

Usage:
    GET /api/method/saathimart.api.sse.order_status_stream?order_id=SM-ORD-2026-00001

Returns a streaming response with event types:
    - status_changed: When order status changes
    - heartbeat: Every 30s to keep connection alive
    - error: On errors
"""
import json
import time
import threading

import frappe
from frappe import _
from frappe.utils import now_datetime

from saathimart.api.responses import handle_api_errors

# In-memory store for SSE connections (production would use Redis pub/sub)
_sse_connections = {}
_sse_lock = threading.Lock()


def _get_sse_key(order_id, client_id):
    """Generate a unique key for an SSE connection."""
    return f"{order_id}:{client_id}"


def _notify_sse_clients(order_id, event_type, data):
    """Notify all SSE clients subscribed to an order about a status change."""
    with _sse_lock:
        key_pattern = f"{order_id}:"
        for key in list(_sse_connections.keys()):
            if key.startswith(key_pattern):
                try:
                    client = _sse_connections[key]
                    client["queue"].append({"event": event_type, "data": data})
                except Exception:
                    pass


@frappe.whitelist(allow_guest=True)
@handle_api_errors
def order_status_stream(order_id, client_id=None):
    """
    SSE endpoint for real-time order status updates.
    
    Args:
        order_id: The order ID to subscribe to
        client_id: Optional client identifier (generated if not provided)
    
    Returns:
        Streaming response with SSE events
    """
    import uuid
    
    if not client_id:
        client_id = str(uuid.uuid4())[:8]
    
    # Validate order exists
    if not frappe.db.exists("Order", order_id):
        frappe.throw(_("Order not found"), frappe.DoesNotExistError)
    
    # For guest tracking, validate phone number
    order_doc = frappe.get_doc("Order", order_id)
    if frappe.session.user == "Guest":
        # Guest users need to provide phone for tracking
        customer_phone = frappe.request.args.get("customer_phone", "").strip()
        stored_phone = (order_doc.customer_phone or "").strip()
        if not stored_phone or not customer_phone or stored_phone != customer_phone:
            frappe.throw(_("Order not found"), frappe.DoesNotExistError)
    
    # Set up SSE connection
    key = _get_sse_key(order_id, client_id)
    queue = []
    
    with _sse_lock:
        _sse_connections[key] = {
            "queue": queue,
            "order_id": order_id,
            "client_id": client_id,
            "created_at": time.time(),
        }
    
    def generate():
        """Generator function for SSE streaming."""
        try:
            # Send initial connection event
            yield f"event: connected\ndata: {json.dumps({'order_id': order_id, 'client_id': client_id})}\n\n"
            
            # Send current status
            current_status = {
                "status": order_doc.status,
                "payment_status": order_doc.payment_status,
                "last_updated": str(order_doc.modified),
            }
            yield f"event: status_changed\ndata: {json.dumps(current_status)}\n\n"
            
            # Stream events
            start_time = time.time()
            heartbeat_interval = 30  # seconds
            last_heartbeat = start_time
            
            while True:
                # Check for new events
                if queue:
                    event = queue.pop(0)
                    yield f"event: {event['event']}\ndata: {json.dumps(event['data'])}\n\n"
                
                # Send heartbeat if needed
                current_time = time.time()
                if current_time - last_heartbeat >= heartbeat_interval:
                    yield f"event: heartbeat\ndata: {json.dumps({'timestamp': now_datetime().isoformat()})}\n\n"
                    last_heartbeat = current_time
                
                # Check if client disconnected (after 5 minutes)
                if current_time - start_time > 300:
                    yield f"event: timeout\ndata: {json.dumps({'message': 'Connection timeout'})}\n\n"
                    break
                
                # Sleep briefly to avoid busy waiting
                time.sleep(0.5)
                
        except GeneratorExit:
            pass
        finally:
            # Clean up connection
            with _sse_lock:
                if key in _sse_connections:
                    del _sse_connections[key]
    
    # Return streaming response
    from werkzeug.wrappers import Response
    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # Disable nginx buffering
        }
    )


def on_order_status_changed(order_id, new_status, old_status):
    """
    Hook called when an order status changes.
    Notifies all SSE clients subscribed to this order.
    
    This function should be called from order status update functions.
    """
    event_data = {
        "status": new_status,
        "old_status": old_status,
        "order_id": order_id,
        "timestamp": now_datetime().isoformat(),
    }
    
    # Notify SSE clients in background
    try:
        from saathimart.api.utils import safe_enqueue
        safe_enqueue(
            _notify_sse_clients,
            order_id=order_id,
            event_type="status_changed",
            data=event_data,
            queue="short",
        )
    except Exception:
        # Don't fail the main request if SSE notification fails
        frappe.log_error(frappe.get_traceback(), f"SSE notification failed for order {order_id}")


def cleanup_stale_connections():
    """Cron: Remove stale SSE connections older than 10 minutes."""
    with _sse_lock:
        stale_keys = [
            key for key, client in _sse_connections.items()
            if time.time() - client["created_at"] > 600  # 10 minutes
        ]
        for key in stale_keys:
            del _sse_connections[key]
    
    return {"cleaned": len(stale_keys)}
