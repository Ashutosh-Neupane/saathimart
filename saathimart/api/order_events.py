"""
Event sourcing for orders — every state change is recorded in an
immutable event log. Enables audit trail, timeline view, and
replay capability.
"""
import frappe
from frappe import _
import json as cjson
from frappe.utils import now_datetime


def record_order_event(order_id, event_type, data=None, actor=None):
    """Record an order event in the event log.

    Args:
        order_id: Order document name
        event_type: e.g. 'created', 'paid', 'status_changed', 'item_added'
        data: dict with event-specific data
        actor: who performed the action (User email or 'system')
    """
    try:
        event = frappe.new_doc("Order Event Log")
        event.order_id = order_id
        event.event_type = event_type
        event.data = cjson.dumps(data or {}, default=str)
        event.actor = actor or frappe.session.user
        event.timestamp = now_datetime()
        event.insert(ignore_permissions=True)
    except Exception:
        frappe.log_error(frappe.get_traceback(), f"Order event log failed: {order_id}")


def get_order_timeline(order_id):
    """Return the full timeline of events for an order."""
    events = frappe.get_all(
        "Order Event Log",
        filters={"order_id": order_id},
        fields=["event_type", "data", "actor", "timestamp"],
        order_by="timestamp asc",
    )

    timeline = []
    for e in events:
        try:
            data = cjson.loads(e.data) if e.data else {}
        except Exception:
            data = {}

        timeline.append({
            "type": e.event_type,
            "data": data,
            "actor": e.actor,
            "timestamp": str(e.timestamp),
            "description": _describe_event(e.event_type, data),
        })

    return timeline


def _describe_event(event_type, data):
    """Human-readable description of an order event."""
    descriptions = {
        "created": "Order placed by {actor}",
        "paid": "Payment received — Rs {amount}",
        "status_changed": "Status changed: {old_status} → {new_status}",
        "item_added": "Item added: {product} × {qty}",
        "item_removed": "Item removed: {product}",
        "vendor_assigned": "Assigned to vendor: {vendor}",
        "vendor_accepted": "Vendor {vendor} accepted the order",
        "dispatched": "Order dispatched by {vendor}",
        "delivered": "Order delivered",
        "cancelled": "Order cancelled — {reason}",
        "refund_requested": "Referral requested — Rs {amount}",
        "refund_processed": "Refund processed — Rs {amount}",
    }

    template = descriptions.get(event_type, event_type)
    try:
        return template.format(**data)
    except (KeyError, TypeError):
        return event_type


# Integration hooks — call record_order_event from key order actions

def on_order_created(doc, method):
    """Hook: after Order insert."""
    record_order_event(doc.name, "created", {
        "customer": doc.customer_name,
        "total": doc.grand_total,
        "items_count": len(doc.items or []),
    })


def on_order_paid(doc, method):
    """Hook: fires on every Order update, but only records an event on the
    Pending->Paid transition — without this guard, wiring it to on_update
    would insert a duplicate "paid" log row on every later save while
    payment_status stays Paid."""
    if doc.payment_status != "Paid":
        return
    before_save = doc.get_doc_before_save()
    if before_save and before_save.payment_status == "Paid":
        return
    record_order_event(doc.name, "paid", {
        "amount": doc.grand_total,
        "method": doc.payment_method,
        "reference": doc.payment_reference,
    })
    # Push notification: payment confirmed
    try:
        from saathimart.api.push_notifications import send_order_notification
        send_order_notification(doc.user, doc.name, "Payment Received")
    except Exception:
        pass  # Don't break order flow if notification fails
