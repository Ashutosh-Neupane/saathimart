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


def _get_watermark(vendor_name):
    """Durable per-vendor watermark.

    The DB column is the source of truth; the Redis copy only accelerates
    reads. mark_processed() writes both, but the Redis key carries an expiry
    and any Redis restart/flush drops it sooner — so a cache MISS (None) must
    fall back to the DB exactly like an error does. Treating a miss as 0 made
    verify_sequence() see a phantom gap against sequence counters that kept
    climbing and held every future event for the vendor FOREVER — a total,
    silent sync stall (events stayed Queued, nothing logged).
    """
    try:
        cached = frappe.cache().get_value(f"sm_last_processed_seq:{vendor_name}")
        if cached is not None:
            return cached
    except Exception:
        pass
    return frappe.db.get_value("Vendor", vendor_name, "last_processed_event_seq") or 0


def verify_sequence(vendor_name, expected_seq):
    """Check if an event with this sequence can be processed.

    Returns True if processing should proceed (no gap).
    Returns False if there is a gap another queued event can still fill.
    """
    last_processed = _get_watermark(vendor_name)

    if expected_seq <= last_processed:
        # Already processed (duplicate) — skip
        return True  # idempotent — let vendor's idempotency handle it

    if expected_seq == last_processed + 1:
        # Exactly the next expected sequence — process it
        return True

    # Gap detected. But if no queued event for this vendor carries an
    # earlier sequence, nothing can EVER fill the gap — the missing events
    # were delivered before a watermark reset (Redis flush/restart) and are
    # gone from the queue. Holding on a phantom gap stalls the vendor's
    # entire queue permanently, so deliver; a real out-of-order window
    # always has an earlier Queued event holding the door open.
    earlier_queued = frappe.db.count(
        "Webhook Event",
        {
            "target_vendor": vendor_name,
            "status": "Queued",
            "event_seq": ["<", expected_seq],
        },
    )
    return earlier_queued == 0


def mark_processed(vendor_name, seq):
    """Mark a sequence as processed, advancing the watermark."""
    # DB first: it is the authoritative watermark (see _get_watermark).
    try:
        frappe.db.set_value(
            "Vendor", vendor_name, "last_processed_event_seq", seq
        )
    except Exception:
        pass
    # Cache copy only accelerates reads; _get_watermark() falls back to the
    # DB row on miss, so expiry or a Redis flush can never rewind progress.
    try:
        frappe.cache().set_value(
            f"sm_last_processed_seq:{vendor_name}", seq, expires_in_sec=604800
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
    last_seq = _get_watermark(vendor_name)

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
