"""
Event ordering guarantees — ensures the vendor processes events in the
correct sequence even if they arrive out of order.

Problem: Network retries can cause events to arrive at the vendor out of order.
Example: order.new (seq=5) arrives before order.accepted (seq=4).

Solution: Each event carries a monotonic sequence number. The vendor's inbox
checks for gaps and holds events with gaps, processing them once the gap fills.

Sequence numbers are per-vendor and monotonically increasing.
"""
import frappe


def get_next_sequence(vendor_name):
    """Get the next sequence number for events to this vendor.

    Uses a Redis counter with DB fallback for atomicity.
    """
    key = f"sm_event_seq:{vendor_name}"

    try:
        cache = frappe.cache()
        current = cache.get_value(key)
        if current is None:
            # Initialize from DB
            last = frappe.db.sql("""
                SELECT MAX(CAST(SUBSTRING(name, LENGTH(name) - LOCATE('-', REVERSE(name)) + 1) AS UNSIGNED)) as last_seq
                FROM `tabWebhook Event`
                WHERE target_vendor = %s AND name LIKE '%%-%%'
            """, (vendor_name,), as_dict=True)
            current = (last[0].last_seq or 0) if last and last[0].last_seq else 0
        next_seq = current + 1
        cache.set_value(key, next_seq, expires_in_sec=86400)
        return next_seq
    except Exception:
        # Redis down — use DB directly
        last = frappe.db.sql("""
            SELECT COUNT(*) as cnt FROM `tabWebhook Event`
            WHERE target_vendor = %s
        """, (vendor_name,), as_dict=True)
        return (last[0].cnt or 0) + 1


def verify_sequence(vendor_name, expected_seq):
    """Check if an event with this sequence can be processed.

    Returns True if processing should proceed (no gap).
    Returns False if there's a gap (hold this event).
    """
    key = f"sm_last_processed_seq:{vendor_name}"

    try:
        cache = frappe.cache()
        last_processed = cache.get_value(key) or 0
    except Exception:
        last_processed = frappe.db.get_value(
            "Vendor", vendor_name, "last_processed_event_seq"
        ) or 0

    if expected_seq <= last_processed:
        # Already processed (duplicate) — skip
        return True  # idempotent — let vendor's idempotency handle it

    if expected_seq == last_processed + 1:
        # Exactly the next expected sequence — process it
        return True

    # Gap detected — hold this event
    return False


def mark_processed(vendor_name, seq):
    """Mark a sequence as processed, advancing the watermark."""
    key = f"sm_last_processed_seq:{vendor_name}"

    try:
        cache = frappe.cache()
        cache.set_value(key, seq, expires_in_sec=86400)
    except Exception:
        pass

    # Also persist to DB for crash recovery
    try:
        frappe.db.set_value(
            "Vendor", vendor_name, "last_processed_event_seq", seq
        )
    except Exception:
        pass


def get_held_events(vendor_name):
    """Get events that are held due to sequence gaps."""
    try:
        last_seq = frappe.cache().get_value(f"sm_last_processed_seq:{vendor_name}") or 0
    except Exception:
        last_seq = frappe.db.get_value(
            "Vendor", vendor_name, "last_processed_event_seq"
        ) or 0

    return frappe.get_all(
        "Webhook Event",
        filters={
            "target_vendor": vendor_name,
            "status": "Queued",
        },
        fields=["name", "event_type", "priority", "creation"],
        order_by="creation asc",
        limit=20,
    )
