"""
Event publisher — pushes events to Redis and drains the SM Webhook Event queue.

Flow:
  1. doc_event hook calls on_order_created / on_product_updated etc.
  2. These enqueue an SM Webhook Event record (status=Queued) and immediately
     schedule its delivery on a background worker (see _schedule_immediate_delivery)
     — delivery normally happens within a second or two of the triggering
     action, not on the next cron tick.
  3. drain_event_queue (cron every 2 min) is the fallback/retry sweep: it
     picks up anything still Queued (the instant delivery failed, the worker
     pool was backed up, etc.) and:
     a. Publishes to Redis pub/sub channel (for real-time subscribers).
     b. POSTs to vendor frappe_site_url if configured.
  4. Vendor sites can also poll via GET /api/method/saathimart.api.events.poll.
"""
import json
import uuid
import urllib.parse
from datetime import datetime, timezone

import frappe
import requests
from frappe.utils import now_datetime, add_to_date, flt

from saathimart.api.utils import safe_enqueue


# ── Internal helpers ──────────────────────────────────────────────────────────

def _get_next_vendor_event_seq(vendor):
    """
    Return the next monotonically increasing event sequence for a vendor.

    Does the increment as a single atomic UPDATE rather than Python-level
    get-then-set — the previous version (SELECT last_event_seq, then
    frappe.db.set_value(last + 1)) raced under concurrent events for the
    same vendor: two requests could both read the same `last`, then both
    write the same `last + 1`, handing out a duplicate event_seq. An UPDATE
    holds the row lock for the read-modify-write, so concurrent callers
    serialize instead of racing.
    """
    frappe.db.sql(
        "UPDATE `tabVendor` SET last_event_seq = COALESCE(last_event_seq, 0) + 1 WHERE name = %s",
        (vendor,),
    )
    return frappe.db.get_value("Vendor", vendor, "last_event_seq")


def _enqueue(event_type, payload, target_site=None, target_vendor=None, event_id=None):
    """
    Create an SM Webhook Event record for async delivery. Idempotent — if
    an event with the same event_id already exists, it is not duplicated.
    Also schedules immediate delivery (see _schedule_immediate_delivery) so
    a vendor doesn't wait on the next drain_event_queue cron tick — that
    cron becomes the retry/fallback sweep for anything the instant path
    missed, rather than the only delivery path.
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

    if doc.target_site:
        _schedule_immediate_delivery(doc.name)


def _schedule_immediate_delivery(event_name):
    """
    Enqueue delivery of a single event right away instead of waiting for the
    next drain_event_queue cron tick.

    enqueue_after_commit=True defers the actual RQ job until this DB
    transaction commits — without it, a worker on a different DB connection
    could pick the job up and query for a Webhook Event row that isn't
    visible yet (this call typically runs inside a doc_event hook, i.e.
    inside the same transaction as the order/status change that triggered
    it). job_id + deduplicate=True means if drain_event_queue's cron sweep
    also picks up this same still-Queued row before this job has run (a
    narrow window — only possible if the worker pool is backed up), only
    one delivery attempt actually gets queued.
    """
    settings = frappe.get_single("Settings")
    secret = settings.get_password("webhook_secret", raise_exception=False) or ""
    max_retries = settings.max_webhook_retries or 3
    safe_enqueue(
        "saathimart.events.publisher._deliver_event_async",
        event_name=event_name,
        secret=secret,
        max_retries=max_retries,
        queue="default",
        enqueue_after_commit=True,
        job_id=f"deliver-webhook-event-{event_name}",
        deduplicate=True,
    )


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
            "warehouse": getattr(f, "warehouse", "") or "",
            "warehouse_distance_km": getattr(f, "warehouse_distance_km", 0) or 0,
            "items": [
                {"product": i.product, "qty": i.qty, "rate": i.rate}
                for i in vendor_items
            ],
        }, target_site=vendor_url, target_vendor=f.vendor,
           event_id=f"order.created.{doc.name}.{f.vendor}")


def publish_payment_received(order_id, amount=None, gateway="", reference=""):
    """
    A payment succeeded for this Order (eSewa callback, the status-poll cron,
    or an admin applying a payment). Push payment.received to every vendor
    with a fulfillment row so their site records the money — creating a real
    ERPNext Payment Entry against that vendor's Sales Order and unblocking
    acceptance of prepaid orders (consumed by saathimart_vendor.api.receive.
    _handle_payment_received).
    """
    try:
        doc = frappe.get_doc("Order", order_id)
    except Exception:
        frappe.log_error(frappe.get_traceback(), f"payment.received: order {order_id} not found")
        return

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
        _enqueue("payment.received", {
            "order_id": doc.name,
            "vendor_id": f.vendor,
            # Per-vendor slice of what was collected — a mixed multi-vendor
            # cart must not show each vendor the whole order's money.
            "amount": flt(f.subtotal) if f.subtotal is not None else flt(amount),
            "grand_total": flt(doc.grand_total),
            "gateway": gateway or doc.payment_method,
            "reference": reference or doc.payment_reference,
            "customer_name": doc.customer_name,
        }, target_site=vendor_url, target_vendor=f.vendor,
           event_id=f"payment.received.{doc.name}.{f.vendor}")


def on_product_created(doc, method):
    """
    A new Product was just created on the hub. Broadcast it to every vendor
    with a reachable site so a vendor who already stocks this physical item
    (i.e. it's in their ERPNext Item Barcode records under some Item of
    theirs) gets auto-mapped with zero manual work — see
    saathimart_vendor.api.receive._handle_new_product. Vendors who don't
    already carry it get nothing; this is not a "please go stock this"
    notification, it only ever acts when there's already a real barcode
    match on the vendor's side.

    Requires Product.sku to be set — that's the only barcode-like field
    available on a brand-new Product (Vendor Listing.barcode doesn't exist
    yet, nothing has been listed against this product by anyone).

    The actual per-vendor fan-out (_broadcast_new_product) runs in a
    background worker rather than here: looping over every vendor inline
    would make every single product save do O(vendor count) DB writes
    before the admin's save() call even returns — noticeable once there
    are more than a handful of vendors, and especially bad for anything
    that bulk-creates products (e.g. scripts/seed.py inserting many
    Products in a loop, each one triggering this hook).
    """
    if not doc.sku:
        return

    safe_enqueue(
        "saathimart.events.publisher._broadcast_new_product",
        product_id=doc.name,
        barcode=doc.sku,
        product_name=doc.product_name,
        queue="short",
        enqueue_after_commit=True,
        job_id=f"broadcast-new-product-{doc.name}",
        deduplicate=True,
    )


def _broadcast_new_product(product_id, barcode, product_name):
    """
    Background worker (see on_product_created). Notifies only the vendors
    who have actually told the hub they can supply this exact barcode
    (Vendor Barcode Index, populated by vendors pushing barcode.register
    whenever they add an ERPNext Item Barcode — see
    saathimart-vendor/event_handlers/mapping.py) — NOT every vendor.

    This used to loop over every vendor with a reachable site regardless of
    whether they had anything to do with this product: at 1,000 vendors,
    every single product creation generated 1,000 Webhook Event rows and
    1,000 outbound HTTP deliveries, the overwhelming majority of which
    would find no matching barcode and do nothing. Matching against the
    index instead turns that into a handful of targeted notifications (in
    the common case, zero) — no fan-out, no thundering herd on
    create_vendor_listing, no event-table bloat.

    The match count itself is *not* bounded, though — a barcode genuinely
    carried by every vendor (a universally-stocked item) still returns
    every vendor from the index, and notifying all of them is correct, not
    wasteful (every one of those really does need mapping). What would
    still be wrong is doing that notification as one long sequential loop
    in a single background job — same "one worker, unbounded loop, lost or
    partial on a crash/timeout" risk as the original broadcast, just gated
    behind a real-match-count instead of the full vendor table. So this
    chunks matches the same way reconcile_stock() chunks mapping batches:
    each chunk is its own small, independently-retryable background job.
    """
    matches = frappe.get_all(
        "Vendor Barcode Index",
        filters={"barcode": barcode},
        fields=["vendor"],
    )
    if not matches:
        return

    chunk_size = 50
    for i in range(0, len(matches), chunk_size):
        chunk = [m.vendor for m in matches[i:i + chunk_size]]
        safe_enqueue(
            "saathimart.events.publisher._notify_vendors_of_matching_product_chunk",
            vendors=chunk,
            product_id=product_id,
            barcode=barcode,
            product_name=product_name,
            queue="short",
            job_id=f"broadcast-new-product-chunk-{product_id}-{i // chunk_size}",
            deduplicate=True,
        )


def _notify_vendors_of_matching_product_chunk(vendors, product_id, barcode, product_name):
    """One bounded chunk of _broadcast_new_product's matches, its own background job."""
    for vendor in vendors:
        _notify_vendor_of_matching_product(vendor, product_id, barcode, product_name)


def _notify_vendor_of_matching_product(vendor, product_id, barcode, product_name):
    """Send a single, targeted product.new to exactly one vendor that's confirmed to carry this barcode."""
    vendor_url = frappe.db.get_value("Vendor", vendor, "frappe_site_url")
    if not vendor_url:
        return
    _enqueue("product.new", {
        "product_id": product_id,
        "barcode": barcode,
        "product_name": product_name,
    }, target_site=vendor_url, target_vendor=vendor,
       event_id=f"product.new.{product_id}.{vendor}")


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


def on_vendor_listing_changed(doc, method):
    """
    Keep Product.display_price/display_compare_price in sync with the
    cheapest active Vendor Listing, whenever any Vendor Listing for this
    product is created, updated, or deleted (hooked here — a single
    doctype-level hook — rather than in every individual call site that
    writes a Vendor Listing price, which is exactly the kind of duplicated
    logic that let saathimart.api.home's bestsellers/recommended sections
    drift out of sync with reality and 500/silently-zero for a while).

    display_price exists purely to make bulk list/browse reads (homepage
    rails, search results) a plain field read instead of a join or a
    separate query per section — modelled on how saathi_middleware keeps
    price directly on the row it queries. Vendor Listing stays the actual
    source of truth: checkout, the product detail page, and anything
    vendor-specific still resolve price from Vendor Listing directly, this
    cache is display-only and is never read at checkout time.
    """
    product = doc.product
    if not product:
        return

    filters = {"product": product, "status": "Active"}
    if method == "on_trash":
        # The row being deleted still physically exists in the DB at the
        # point on_trash fires (the actual DELETE happens after) — exclude
        # it explicitly so a deleted listing's price doesn't linger in the
        # cache for one extra recompute.
        filters["name"] = ["!=", doc.name]

    # Plain row fetch + Python-side min/max — dict-aggregate syntax
    # ({"MIN": "price", "as": "price"}) crashes the v15 query engine
    # ('NoneType' object has no attribute 'fieldtype'), which took down
    # every Vendor Listing save/trash and most of the test suite.
    listings = frappe.get_all(
        "Vendor Listing",
        filters=filters,
        fields=["price", "compare_price"],
        limit_page_length=0,
    )
    prices = [l.price for l in listings if l.price is not None]
    compare_prices = [l.compare_price for l in listings if l.compare_price is not None]
    frappe.db.set_value("Product", product, {
        "display_price": min(prices) if prices else 0,
        "display_compare_price": max(compare_prices) if compare_prices else 0,
    }, update_modified=False)


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
        safe_enqueue(
            "saathimart.events.publisher._deliver_event_async",
            event_name=evt.name,
            secret=secret,
            max_retries=max_retries,
            queue="default",
            job_id=f"deliver-webhook-event-{evt.name}",
            deduplicate=True,
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

        target_url = evt.target_site
        host_header = None
        if target_vendor:
            vendor_site_url = frappe.db.get_value("Vendor", target_vendor, "frappe_site_url") or ""
            if vendor_site_url:
                host_header = urllib.parse.urlparse(vendor_site_url).hostname
            parsed = urllib.parse.urlparse(target_url)
            if parsed.hostname in ("localhost", "vendor1.localhost", "vendor2.localhost", "vendor3.localhost"):
                target_url = parsed._replace(netloc="vendors:8000").geturl()

        # Sign the exact bytes we send: HMAC-SHA256(secret, "<ts>.<body>").
        # The vendor recomputes it from the raw body — the secret itself
        # never crosses the wire. Legacy bare X-SM-Secret header removed.
        ts = str(int(datetime.now(timezone.utc).timestamp()))
        body = json.dumps({"event": evt.event_type, "payload": json.loads(evt.payload or "{}")})
        from saathimart.api.utils import compute_hmac_signature

        headers = {
            "X-SM-Timestamp": ts,
            "X-SM-Signature": compute_hmac_signature(vendor_secret, ts, body),
            "Content-Type": "application/json",
        }
        if host_header:
            headers["Host"] = host_header

        resp = requests.post(
            f"{target_url}/api/method/saathimart_vendor.api.receive.receive_from_hub",
            data=body,
            headers=headers,
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
