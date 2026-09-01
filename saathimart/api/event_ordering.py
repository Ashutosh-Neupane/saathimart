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
    """
    Get the next sequence number for events to this vendor.

    Delegates to publisher._get_next_vendor_event_seq — the atomic
    `UPDATE tabVendor SET last_event_seq = last_event_seq + 1` that
    _enqueue() already uses for every real event. This used to be a
    separate Redis-counter-with-COUNT(*)-fallback implementation, which
    had a real bug: the COUNT(*) fallback (used whenever Redis is down)
    undercounts once dead_letter.archive_old_events() (weekly) has deleted
    old Webhook Event rows, handing out a sequence number that collides
    with one already delivered. The Vendor.last_event_seq column doesn't
    have that problem — it's a running counter, not derived from row
    counts — so delegating removes the bug and the duplicate logic in one
    move rather than patching the COUNT(*) query.
    """
    from saathimart.events.publisher import _get_next_vendor_event_seq
    return _get_next_vendor_event_seq(vendor_name)


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
    """
    Get events queued for this vendor whose event_seq is ahead of the
    watermark — i.e. actually held back by _deliver_event's gap check
    (see events/publisher.py), not just "queued". last_seq used to be
    computed and then never applied to the filter, so this returned every
    Queued event for the vendor regardless of ordering, not the held ones
    it's named for.
    """
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
            "event_seq": [">", last_seq],
        },
        fields=["name", "event_type", "priority", "creation", "event_seq"],
        order_by="event_seq asc",
        limit=20,
    )
