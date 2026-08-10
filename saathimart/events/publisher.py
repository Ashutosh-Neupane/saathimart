"""
Event publisher — pushes events to Redis and drains the SM Webhook Event queue.

Flow:
  1. doc_event hook calls on_order_created / on_product_updated etc.
  2. These enqueue an SM Webhook Event record (status=Queued).
  3. drain_event_queue (cron every 2 min) picks Queued events and:
     a. Publishes to Redis pub/sub channel (for real-time subscribers).
     b. POSTs to vendor frappe_site_url if configured.
  4. Vendor sites can also poll via GET /api/method/saathimart.api.events.poll.
"""
import json
import uuid

import frappe
import requests
from frappe.utils import now_datetime, add_to_date


# ── Internal helpers ──────────────────────────────────────────────────────────

def _get_next_vendor_event_seq(vendor):
    """Return the next monotonically increasing event sequence for a vendor."""
    last = frappe.db.get_value("Vendor", vendor, "last_event_seq") or 0
    seq = last + 1
    frappe.db.set_value("Vendor", vendor, "last_event_seq", seq)
    return seq


def _enqueue(event_type, payload, target_site=None, target_vendor=None, event_id=None):
    """
    Create an SM Webhook Event record for async delivery. Idempotent — if
    an event with the same event_id already exists, it is not duplicated.
    """
    if not event_id:
        event_id = str(uuid.uuid4())

    existing = frappe.db.get_value("Webhook Event", {"event_id": event_id}, "name")
    if existing:
        return

    event_seq = None
    if target_vendor:
        event_seq = _get_next_vendor_event_seq(target_vendor)

    doc = frappe.new_doc("Webhook Event")
    doc.event_type = event_type
    doc.event_id = event_id
    doc.event_seq = event_seq
    doc.target_site = target_site or ""
    doc.target_vendor = target_vendor or ""
    doc.payload = json.dumps(payload, default=str)
    doc.insert(ignore_permissions=True)


def _publish_to_redis(event_type, payload):
    """Publish to Redis pub/sub for real-time subscribers."""
    try:
        settings = frappe.get_single("Settings")
        channel = settings.event_channel or "saathimart:events"
        message = json.dumps({"event": event_type, "data": payload, "ts": str(now_datetime())})
        frappe.cache().publish(channel, message)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Redis publish failed")


# ── Doc event hooks ───────────────────────────────────────────────────────────

def on_order_created(doc, method):
    """
    A new Order was just inserted (checkout). Push it to every vendor with a
    Vendor Fulfillment row on this order so each can accept/prepare their own
    slice — this is what saathimart_vendor.api.receive._handle_new_order
    consumes. Each vendor is only sent their own items and their own
    fulfillment subtotal, never the other vendors' share of a mixed cart.
    """
    payload = {
        "order_id": doc.name,
        "vendor": doc.vendor,
        "status": doc.status,
        "grand_total": doc.grand_total,
        "source_site": doc.source_site,
        "event_seq": None,  # set by _enqueue if target_vendor is known
    }
    _publish_to_redis("order.created", payload)

    fulfillments = list(doc.vendor_fulfillments or [])
    if not fulfillments and doc.vendor:
        # Legacy path — order predates Vendor Fulfillment rows.
        fulfillments = [frappe._dict(vendor=doc.vendor, subtotal=doc.grand_total)]

    for f in fulfillments:
        if not f.vendor:
            continue
        vendor_url = frappe.db.get_value("Vendor", f.vendor, "frappe_site_url")
        if not vendor_url:
            continue
        vendor_items = [i for i in doc.items if i.vendor == f.vendor]
        _enqueue("order.new", {
            "order_id": doc.name,
            "customer_name": doc.customer_name,
            "customer_phone": doc.customer_phone,
            "delivery_address": doc.delivery_address,
            "delivery_lat": doc.delivery_lat,
            "delivery_lng": doc.delivery_lng,
            "grand_total": f.subtotal,
            "payment_method": doc.payment_method,
            "payment_status": doc.payment_status,
            "items": [
                {"product": i.product, "qty": i.qty, "rate": i.rate}
                for i in vendor_items
            ],
        }, target_site=vendor_url, target_vendor=f.vendor,
           event_id=f"order.created.{doc.name}.{f.vendor}")


def on_order_updated(doc, method):
    """
    Fires on every doc.save() after insert — i.e. hub/admin-driven status
    changes via api.orders.update_order_status(). Vendor-driven changes
    (Confirmed/Dispatched/Delivered, pushed the other direction via
    api.events.receive) use frappe.db.set_value and never reach here, so
    this can't ping-pong a status straight back to the vendor that sent it.
    Only "Cancelled" (hub/admin/customer-initiated) needs to be told to the
    vendor — the other statuses are things the vendor told the hub, not the
    other way round. Every vendor with an active (non-Delivered) fulfillment
    on this order is notified, not just the first one.
    """
    payload = {"order_id": doc.name, "status": doc.status, "vendor": doc.vendor}
    _publish_to_redis("order.updated", payload)
    if doc.status != "Cancelled":
        return

    fulfillments = list(doc.vendor_fulfillments or [])
    if not fulfillments and doc.vendor:
        fulfillments = [frappe._dict(vendor=doc.vendor)]

    for f in fulfillments:
        if not f.vendor or f.status == "Delivered":
            continue
        vendor_url = frappe.db.get_value("Vendor", f.vendor, "frappe_site_url")
        if not vendor_url:
            continue
        _enqueue("order.cancel", {
            "order_id": doc.name,
            "reason": "Cancelled on SaathiMart",
        }, target_site=vendor_url, target_vendor=f.vendor,
           event_id=f"order.updated.{doc.name}.{f.vendor}")


def on_product_updated(doc, method):
    _publish_to_redis("product.updated", {"product_id": doc.name, "status": doc.status})


def on_product_deleted(doc, method):
    _publish_to_redis("product.deleted", {"product_id": doc.name})


def on_vendor_updated(doc, method):
    _publish_to_redis("vendor.updated", {"vendor_id": doc.name, "status": doc.status})


# ── Scheduled: drain queue ────────────────────────────────────────────────────

def drain_event_queue():
    """Cron every 2 min — deliver Queued webhook events to vendor sites."""
    settings = frappe.get_single("Settings")
    max_retries = settings.max_webhook_retries or 3
    secret = settings.get_password("webhook_secret", raise_exception=False) or ""

    events = frappe.get_list(
        "Webhook Event",
        filters={"status": "Queued", "target_site": ["!=", ""]},
        fields=["name", "event_type", "target_site", "payload", "retry_count", "target_vendor"],
        limit=50,
        order_by="creation asc",
    )

    for evt in events:
        frappe.enqueue(
            "saathimart.events.publisher._deliver_event_async",
            event_name=evt.name,
            secret=secret,
            max_retries=max_retries,
            queue="default",
        )


def _deliver_event_async(event_name, secret, max_retries):
    """Deliver a single webhook event in a background worker."""
    evt = frappe.get_doc("Webhook Event", event_name)
    _deliver_event(evt, secret, max_retries)


def _deliver_event(evt, secret, max_retries):
    try:
        target_vendor = getattr(evt, "target_vendor", None) or ""
        vendor_secret = secret
        if target_vendor:
            vs = frappe.db.get_value("Vendor", target_vendor, "webhook_secret")
            if vs:
                vendor_secret = vs

        resp = requests.post(
            f"{evt.target_site}/api/method/saathimart_vendor.api.receive.receive_from_hub",
            json={"event": evt.event_type, "payload": json.loads(evt.payload or "{}")},
            headers={"X-SM-Secret": vendor_secret, "Content-Type": "application/json"},
            timeout=10,
        )
        status = "Sent" if resp.ok else "Failed"
        response_text = resp.text[:2000]
    except Exception as e:
        status = "Failed"
        response_text = str(e)[:2000]

    retry_count = (evt.retry_count or 0) + 1
    if status == "Failed" and retry_count < max_retries:
        next_retry = add_to_date(now_datetime(), minutes=2 ** retry_count)
        frappe.db.set_value("Webhook Event", evt.name, {
            "status": "Queued",
            "retry_count": retry_count,
            "next_retry_at": next_retry,
            "response": response_text,
        })
    elif status == "Failed":
        frappe.db.set_value("Webhook Event", evt.name, {
            "status": "Dead",
            "retry_count": retry_count,
            "response": response_text,
            "dead_letter_reason": f"Failed after {retry_count} retries. Last error: {response_text[:500]}",
        })
    else:
        frappe.db.set_value("Webhook Event", evt.name, {
            "status": status,
            "retry_count": retry_count,
            "response": response_text,
        })
    frappe.db.commit()


def flush_failed_webhooks():
    """Hourly — re-queue Failed events that are past their next_retry_at."""
    frappe.db.sql("""
        UPDATE `tabWebhook Event`
        SET status = 'Queued',
            next_retry_at = DATE_ADD(NOW(), INTERVAL 5 MINUTE)
        WHERE status = 'Failed'
          AND next_retry_at <= NOW()
          AND retry_count < 3
    """)
