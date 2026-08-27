"""
Event batching and compression — reduces the number of HTTP calls between
hub and vendor by combining related events.

Problem: During peak hours, 100+ stock.update events fire per vendor.
Each is a separate HTTP POST. This wastes network resources and hits
rate limits.

Solution:
  1. Batch multiple stock updates into a single "stock.batch" event
  2. Compress large payloads with gzip before sending
  3. Batch multiple events into a single "events.batch" envelope

The batch window is configurable (default 30 seconds). Events within
the window are collected, deduplicated, and sent as one request.
"""
import gzip
import json

import frappe
from frappe.utils import now_datetime

BATCH_WINDOW_SECONDS = 30
MAX_BATCH_SIZE = 50  # max events per batch


def compress_payload(payload_bytes):
    """Compress a payload with gzip.

    Returns compressed bytes. Only compresses if the result is smaller.
    """
    try:
        compressed = gzip.compress(payload_bytes, compresslevel=6)
        if len(compressed) < len(payload_bytes) * 0.8:  # only if 20%+ savings
            return compressed, True
        return payload_bytes, False
    except Exception:
        return payload_bytes, False


def decompress_payload(payload_bytes, is_compressed=False):
    """Decompress a gzip payload if needed."""
    if not is_compressed:
        return payload_bytes
    try:
        return gzip.decompress(payload_bytes)
    except Exception:
        return payload_bytes


def should_batch_event(event_type):
    """Check if this event type should be batched.

    Stock updates and analytics events are batchable.
    Payment and order events are not (they're time-critical).
    """
    batchable = {
        "stock.update",
        "analytics",
        "review.new",
        "product.updated",
        "warehouse.sync",
    }
    return event_type in batchable


def batch_stock_events(vendor_name, events):
    """Combine multiple stock.update events into a single batch.

    Merges by product: if the same product appears multiple times,
    the latest quantity wins.

    Returns a single batched event dict.
    """
    merged = {}
    for event in events:
        try:
            payload = json.loads(event.get("payload", "{}"))
            product = payload.get("product")
            if product:
                merged[product] = {
                    "product": product,
                    "stock_qty": payload.get("stock_qty", 0),
                    "warehouse": payload.get("warehouse", "default"),
                }
        except Exception:
            continue

    batch_payload = {
        "event_type": "stock.batch",
        "vendor": vendor_name,
        "items": list(merged.values()),
        "item_count": len(merged),
        "batched_from": len(events),
        "timestamp": str(now_datetime()),
    }

    return batch_payload


def batch_generic_events(events):
    """Combine multiple events of different types into a batch envelope."""
    batch = []
    for event in events:
        try:
            batch.append({
                "event_type": event.get("event_type"),
                "payload": json.loads(event.get("payload", "{}")) if isinstance(event.get("payload"), str) else event.get("payload", {}),
                "event_id": event.get("name"),
            })
        except Exception:
            continue

    return {
        "event_type": "events.batch",
        "events": batch,
        "event_count": len(batch),
        "timestamp": str(now_datetime()),
    }


def create_batch_event(vendor_name, batch_payload, compressed=False):
    """Create a Webhook Event for a batch."""
    event = frappe.new_doc("Webhook Event")
    event.event_type = batch_payload["event_type"]
    event.target_vendor = vendor_name
    event.target_site = frappe.db.get_value("Vendor", vendor_name, "webhook_url") or ""
    event.payload = json.dumps(batch_payload, default=str)
    event.status = "Queued"
    event.priority = 3  # NORMAL
    if compressed:
        event.compressed = 1
    event.insert(ignore_permissions=True)
    return event


def unpack_batch_event(batch_payload):
    """Vendor-side: unpack a batch event into individual events.

    Returns list of (event_type, payload_dict) tuples.
    """
    event_type = batch_payload.get("event_type", "")

    if event_type == "stock.batch":
        items = batch_payload.get("items", [])
        return [
            ("stock.update", item) for item in items
        ]

    elif event_type == "events.batch":
        events = batch_payload.get("events", [])
        return [
            (e.get("event_type"), e.get("payload", {})) for e in events
        ]

    else:
        return [(event_type, batch_payload)]


def get_batch_stats():
    """Get batching statistics for monitoring."""
    try:
        batched = frappe.db.count("Webhook Event", {"event_type": "stock.batch"})
        regular_stock = frappe.db.count("Webhook Event", {
            "event_type": "stock.update",
            "creation": (">=", frappe.utils.add_to_date(None, days=-1)),
        })
        return {
            "batch_events_24h": batched,
            "regular_stock_events_24h": regular_stock,
            "estimated_reduction": f"{max(0, regular_stock - batched)} events saved",
        }
    except Exception:
        return {"status": "unavailable"}
