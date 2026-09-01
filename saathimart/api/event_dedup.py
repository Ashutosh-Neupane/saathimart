"""
Event deduplication — prevents the same event from being processed twice.

Uses a sliding window of event fingerprints (hash of event_type + target + payload_key).
The vendor already has idempotency checks, but this catches duplicates at the
hub level before they waste network resources.

Fingerprint TTL: 10 minutes (up from the implicit single-delivery window).
"""
import hashlib
import json

import frappe
from frappe.utils import now_datetime

DEDUP_TTL = 600  # 10 minutes


def event_fingerprint(event_type, target_vendor, payload):
    """Generate a dedup fingerprint for an event."""
    key_str = f"{event_type}:{target_vendor}:{json.dumps(payload, sort_keys=True, default=str)}"
    return hashlib.sha256(key_str.encode()).hexdigest()[:32]


def is_duplicate(event_type, target_vendor, payload):
    """Check if this event was already queued/sent recently.

    Returns True if duplicate (should skip), False if new (should proceed).
    """
    fp = event_fingerprint(event_type, target_vendor, payload)
    cache_key = f"sm_dedup:{fp}"

    try:
        cache = frappe.cache()
        if cache.get_value(cache_key):
            return True
        cache.set_value(cache_key, 1, expires_in_sec=DEDUP_TTL)
        return False
    except Exception:
        # Redis down — fall through to DB-based dedup
        return _db_is_duplicate(event_type, target_vendor, payload)


def mark_delivered(event_type, target_vendor, payload):
    """Mark an event as delivered for dedup tracking."""
    fp = event_fingerprint(event_type, target_vendor, payload)
    cache_key = f"sm_dedup:{fp}"
    try:
        frappe.cache().set_value(cache_key, 1, expires_in_sec=DEDUP_TTL)
    except Exception:
        pass


def _db_is_duplicate(event_type, target_vendor, payload):
    """
    DB-based fallback for dedup when Redis is down. The fingerprint itself
    is never stored anywhere (no schema change needed) — instead this
    re-fingerprints each recent same-type/same-vendor row's own payload and
    compares, which is exactly what the cache-backed path is a fast index
    over.
    """
    try:
        fp = event_fingerprint(event_type, target_vendor, payload)
        cutoff = frappe.utils.add_to_date(now_datetime(), seconds=-DEDUP_TTL)
        candidates = frappe.db.sql(
            "SELECT payload FROM `tabWebhook Event` WHERE event_type = %s "
            "AND target_vendor = %s AND status IN ('Queued', 'Sent') "
            "AND creation >= %s",
            (event_type, target_vendor, cutoff),
            as_dict=True,
        )
        for row in candidates:
            try:
                candidate_payload = json.loads(row.payload or "{}")
            except (TypeError, ValueError):
                continue
            if event_fingerprint(event_type, target_vendor, candidate_payload) == fp:
                return True
        return False
    except Exception:
        return False  # fail open — don't block events if dedup check fails
