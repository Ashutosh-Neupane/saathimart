"""
Event priority system — ensures critical events (payments, order cancellations)
are delivered before lower-priority events (stock updates, analytics).

Priority levels:
  CRITICAL (1): payment.received, order.cancelled — must deliver ASAP
  HIGH (2):     order.new, order.accepted — customer-facing
  NORMAL (3):   order.status_change, stock.update — routine
  LOW (4):      analytics, reviews — can wait

The drain_event_queue cron processes CRITICAL and HIGH events first,
then NORMAL, then LOW. This prevents a flood of stock updates from
blocking payment confirmations.
"""
import frappe

PRIORITY_MAP = {
    "payment.received": 1,
    "payment.failed": 1,
    "order.cancelled": 1,
    "order.new": 2,
    "order.accepted": 2,
    "order.dispatched": 2,
    "order.delivered": 2,
    "order.returned": 2,
    "order.status_change": 3,
    "stock.update": 3,
    "product.updated": 3,
    "warehouse.sync": 3,
    "analytics": 4,
    "review.new": 4,
}

DEFAULT_PRIORITY = 3  # NORMAL


def get_priority(event_type):
    """Get the priority level for an event type."""
    return PRIORITY_MAP.get(event_type, DEFAULT_PRIORITY)


def get_priority_label(priority):
    """Get human-readable label for a priority level."""
    labels = {1: "CRITICAL", 2: "HIGH", 3: "NORMAL", 4: "LOW"}
    return labels.get(priority, "NORMAL")


def set_event_priority(event_name, event_type):
    """Set the priority field on a Webhook Event document."""
    priority = get_priority(event_type)
    frappe.db.set_value("Webhook Event", event_name, "priority", priority)
    return priority


def get_events_by_priority(status="Queued", limit=50):
    """Fetch events ordered by priority (critical first), then by creation time.

    Returns events in delivery order: CRITICAL → HIGH → NORMAL → LOW.
    """
    return frappe.db.sql("""
        SELECT name, event_type, target_vendor, target_site, priority, creation
        FROM `tabWebhook Event`
        WHERE status = %s
        ORDER BY priority ASC, creation ASC
        LIMIT %s
    """, (status, limit), as_dict=True)


def get_priority_stats():
    """Get count of events by priority (for dashboard)."""
    return frappe.db.sql("""
        SELECT priority, status, COUNT(*) as cnt
        FROM `tabWebhook Event`
        WHERE status IN ('Queued', 'Dead', 'Failed')
        GROUP BY priority, status
        ORDER BY priority ASC
    """, as_dict=True)
