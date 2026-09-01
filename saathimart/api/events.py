"""
Events API — vendor sites poll here, and receive inbound webhook events.
"""
import json
import uuid

import frappe
from frappe import _
from frappe.utils import now_datetime, add_to_date

from saathimart.api.constants import VALID_ORDER_TRANSITIONS
from saathimart.api.utils import guest_rate_limit, verify_hub_secret, verify_hub_timestamp, safe_enqueue


def _validate_order_transition(order, new_status):
    """Validate that an order status transition is allowed."""
    old_status = order.status
    if old_status == new_status:
        return True  # idempotent — no transition needed
    allowed = VALID_ORDER_TRANSITIONS.get(old_status, [])
    if new_status not in allowed:
        frappe.throw(_(
            "Invalid order status transition: {0} → {1}. Allowed: {2}"
        ).format(old_status, new_status, ", ".join(allowed) if allowed else "none (terminal)"))
    return True


@frappe.whitelist(allow_guest=True)
def poll(since=None, limit=50):
    """
    Vendor sites call GET /api/method/saathimart.api.events.poll to catch up
    on events they may have missed — a site that was down when
    drain_event_queue tried to push, or an event that exhausted its retries
    and went Dead, previously had no way back into the vendor's hands short
    of someone noticing and manually resyncing. Ordered by event_seq (this
    vendor's own monotonic, gap-free counter — see
    publisher._get_next_vendor_event_seq) rather than wall-clock creation
    time, which doesn't paginate as cleanly and is vulnerable to clock skew.

    Scoped to events targeted at the *calling* vendor only (previously this
    returned every vendor's Sent events to whoever asked — a real
    cross-vendor data leak, not just an inefficiency).

    Headers: X-SM-Secret (or the vendor's own webhook_secret) + X-Vendor-ID.
    `since` — the last event_seq this vendor has already processed
    (Vendor Config.last_hub_event_seq on their side); omit/0 to pull
    everything targeted at this vendor.
    """
    guest_rate_limit("events.poll", limit=100, window_seconds=60)
    verify_hub_secret("events.poll")

    vendor_id = frappe.request.headers.get("X-Vendor-ID", "") if frappe.request else ""
    if not vendor_id:
        frappe.throw(_("X-Vendor-ID header is required"), frappe.AuthenticationError)

    filters = {"target_vendor": vendor_id}
    if since:
        filters["event_seq"] = [">", int(since)]

    events = frappe.get_list(
        "Webhook Event",
        filters=filters,
        fields=["name", "event_type", "event_seq", "payload", "status", "creation"],
        limit=int(limit),
        order_by="event_seq asc",
    )
    return {
        "events": [
            {**e, "payload": json.loads(e.payload or "{}")}
            for e in events
        ],
        "polled_at": str(now_datetime()),
    }


@frappe.whitelist(allow_guest=True)
def bulk_receive(events=None):
    """
    Bulk inbound webhook from vendor sites. Accepts an array of events and
    processes each with the SAME handlers as `receive` — including the
    same idempotency check and Webhook Event audit-trail insert `receive`
    does per event, not just the same dispatch. A bulk delivery isn't
    exempt from either guarantee just because it arrived as one HTTP call
    instead of several: without this, a retried bulk batch (e.g. the
    vendor's HTTP client times out waiting for a response the hub actually
    sent) would silently re-apply every event in it a second time, and
    none of these events would show up in the Webhook Event desk list the
    way every other inbound event does.

    Payload: {"events": [{"event": "stock.receipt", "payload": {...}}, ...]}

    Fast-acks: each event is durably recorded (status=Queued) and its actual
    application (_handle_inbound) is deferred to a background job. This
    endpoint runs on the same worker pool that serves the customer-facing
    Next.js frontend — a burst of vendor pushes running _handle_inbound's DB
    work inline here would compete with that traffic for workers instead of
    just handing off and returning.
    """
    guest_rate_limit("events.bulk_receive", limit=100, window_seconds=60)
    vendor_id = frappe.request.headers.get("X-Vendor-ID", "") if frappe.request else ""
    verify_hub_timestamp(vendor_name=vendor_id or None)
    verify_hub_secret("events.bulk_receive")
    events = events or []
    if not isinstance(events, list):
        frappe.throw(_("events must be a list"))

    results = []
    for evt in events:
        event_name = evt.get("event")
        payload = evt.get("payload") or {}
        event_id = payload.get("event_id") if isinstance(payload, dict) else None

        if event_id and frappe.db.get_value("Webhook Event", {"event_id": event_id}, "name"):
            results.append({"ok": True, "event": event_name, "message": "already_processed"})
            continue

        try:
            doc = frappe.new_doc("Webhook Event")
            doc.event_type = f"inbound.{event_name}"
            doc.event_id = event_id or str(uuid.uuid4())
            doc.payload = json.dumps(payload, default=str)
            doc.status = "Queued"
            doc.insert(ignore_permissions=True)

            safe_enqueue(
                "saathimart.api.events._process_inbound_event",
                webhook_event_name=doc.name,
                queue="default",
                enqueue_after_commit=True,
                deduplicate=True,
                job_id=f"process-inbound-{doc.name}",
            )
            results.append({"ok": True, "event": event_name, "webhook_event": doc.name})
        except Exception as e:
            results.append({"ok": False, "event": event_name, "error": str(e)[:200]})
            frappe.log_error(
                frappe.get_traceback(),
                f"bulk_receive failed for {event_name}",
            )

    frappe.db.commit()
    return {"results": results}


@frappe.whitelist(allow_guest=True)
def receive(event=None, payload=None):
    """
    Inbound webhook from vendor sites back to central hub.
    e.g. vendor confirms order, updates stock, pushes price change.

    Fast-acks the same way bulk_receive does — see its docstring — instead
    of running _handle_inbound inline on a web worker.
    """
    guest_rate_limit("events.receive", limit=1000, window_seconds=60)
    vendor_id = frappe.request.headers.get("X-Vendor-ID", "") if frappe.request else ""
    verify_hub_timestamp(vendor_name=vendor_id or None)
    verify_hub_secret("events.receive")
    if not event:
        frappe.throw(_("event is required"))

    # Idempotency check for order events via event_id
    event_id = payload.get("event_id") if isinstance(payload, dict) else None
    if event_id:
        existing = frappe.db.get_value("Webhook Event", {"event_id": event_id}, "name")
        if existing:
            return {"ok": True, "message": "already_processed"}

    doc = frappe.new_doc("Webhook Event")
    doc.event_type = f"inbound.{event}"
    doc.event_id = event_id or str(uuid.uuid4())
    doc.payload = json.dumps(payload or {}, default=str)
    doc.status = "Queued"
    doc.insert(ignore_permissions=True)

    safe_enqueue(
        "saathimart.api.events._process_inbound_event",
        webhook_event_name=doc.name,
        queue="default",
        enqueue_after_commit=True,
        deduplicate=True,
        job_id=f"process-inbound-{doc.name}",
    )
    return {"ok": True}


def _process_inbound_event(webhook_event_name):
    """
    Background-job counterpart to receive()/bulk_receive() — runs the actual
    order/stock/price application (_handle_inbound) off the web worker pool.
    Mirrors publisher._deliver_event_async's shape: load the Webhook Event
    doc by name, do the work, record the outcome with frappe.db.set_value +
    an explicit commit (a worker job doesn't get the auto-commit a web
    request does).
    """
    evt = frappe.get_doc("Webhook Event", webhook_event_name)
    event = (evt.event_type or "").removeprefix("inbound.")
    payload = json.loads(evt.payload or "{}")
    try:
        _handle_inbound(event, payload)
        frappe.db.set_value("Webhook Event", webhook_event_name, "status", "Sent")
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), f"Inbound event processing failed: {event}")
        frappe.db.set_value("Webhook Event", webhook_event_name, {
            "status": "Failed",
            "dead_letter_reason": str(e)[:500],
        })
    frappe.db.commit()


def _handle_inbound(event, payload):
    if event == "order.confirmed" and payload.get("order_id"):
        _apply_order_status(payload, "Confirmed")

    elif event == "order.preparing" and payload.get("order_id"):
        _apply_order_status(payload, "Preparing")

    elif event == "order.dispatched" and payload.get("order_id"):
        _apply_order_status(payload, "Out for Delivery")

    elif event == "order.delivered" and payload.get("order_id"):
        _apply_order_delivered(payload)

    elif event == "order.cancel" and payload.get("order_id"):
        _apply_order_cancel_from_vendor(payload)

    elif event in ("stock.update", "stock.receipt", "stock.deduct", "stock.adjustment"):
        # New vendor-app payloads carry vendor_id/hub_product/qty_change and
        # go through the per-vendor Vendor Stock ledger. Legacy callers that
        # only send product_id/qty (no vendor) fall back to the old pooled
        # Product.stock_qty update.
        if payload.get("vendor_id") or payload.get("vendor"):
            from saathimart.api.stock import apply_vendor_stock_event
            apply_vendor_stock_event(event, payload)
        elif payload.get("product_id"):
            _apply_stock_receipt(payload)

    elif event == "price.update":
        _apply_price_update(payload)

    elif event == "barcode.register":
        _apply_barcode_register(payload)

    elif event == "barcode.unregister":
        _apply_barcode_unregister(payload)

    elif event == "stock.snapshot_report":
        from saathimart.api.stock_snapshot import record_stock_snapshot_report
        record_stock_snapshot_report(
            payload.get("vendor") or payload.get("vendor_id"),
            payload.get("discrepancies") or [],
        )

    frappe.db.commit()


def _apply_order_status(payload, new_status):
    """
    Apply a vendor-reported order status change with state-machine validation.
    On a multi-vendor order, only the reporting vendor's Vendor Fulfillment
    row is advanced — the whole-order status is then re-derived so it never
    outpaces the slowest vendor. Falls back to the whole-order field when the
    order has no fulfillment rows (pre-multi-vendor orders).
    """
    order_id = payload.get("order_id")
    if not order_id or not frappe.db.exists("Order", order_id):
        frappe.log_error(title="Order Status Update", message=f"order status: unknown order {order_id}")
        return

    from saathimart.api.orders import _find_fulfillment, _recompute_order_status_from_fulfillments

    doc = frappe.get_doc("Order", order_id)
    vendor = payload.get("vendor_id") or payload.get("vendor") or doc.vendor
    fulfillment = _find_fulfillment(doc, vendor)

    if fulfillment:
        if fulfillment.status == new_status:
            return  # idempotent — already in this state
        _validate_order_transition(fulfillment, new_status)
        frappe.db.set_value("Vendor Fulfillment", fulfillment.name, "status", new_status)
        doc.reload()
        _recompute_order_status_from_fulfillments(doc)
    else:
        if doc.status == new_status:
            return  # idempotent — already in this state
        _validate_order_transition(doc, new_status)
        doc.status = new_status
        doc.save(ignore_permissions=True)
        from saathimart.api.notifications import create_order_status_notification
        create_order_status_notification(doc, new_status)


def _apply_order_delivered(payload):
    """
    Vendor confirms delivery (Vendor Order.mark_delivered on their site).
    Finalises that vendor's per-item stock reservations into a real
    deduction. On a multi-vendor order, loyalty earn and the whole-order
    Delivered status only fire once every vendor's fulfillment has reached
    Delivered — one vendor finishing first must not deduct another vendor's
    stock or mark the customer's order as fully delivered.
    """
    order_id = payload["order_id"]
    if not frappe.db.exists("Order", order_id):
        frappe.log_error(title="Order Delivered", message=f"order.delivered: unknown order {order_id}")
        return

    from saathimart.api.orders import _find_fulfillment, _recompute_order_status_from_fulfillments
    from saathimart.api.stock import confirm_deduction

    doc = frappe.get_doc("Order", order_id)
    if doc.status == "Delivered":
        return  # already applied — vendor may retry the push

    vendor = payload.get("vendor_id") or payload.get("vendor") or doc.vendor
    fulfillment = _find_fulfillment(doc, vendor)

    if fulfillment:
        if fulfillment.status == "Delivered":
            return  # this vendor's slice already confirmed — retry, no-op
        for item in doc.items:
            if item.vendor == vendor:
                confirm_deduction(item.vendor, item.product, item.qty, order_id=doc.name)
        frappe.db.set_value("Vendor Fulfillment", fulfillment.name, "status", "Delivered")
        doc.reload()
        became_delivered = _recompute_order_status_from_fulfillments(doc)
    else:
        for item in doc.items:
            if item.vendor:
                confirm_deduction(item.vendor, item.product, item.qty, order_id=doc.name)
        frappe.db.set_value("Order", order_id, "status", "Delivered")
        became_delivered = True
        from saathimart.api.notifications import create_order_status_notification
        create_order_status_notification(doc, "Delivered")

    if became_delivered and doc.payment_method == "COD" and doc.customer_email:
        from saathimart.api.loyalty import earn_points
        earned = earn_points(doc.customer_email, doc.name, doc.grand_total)
        if earned:
            frappe.db.set_value("Order", order_id, "loyalty_points_earned", earned)


def _apply_order_cancel_from_vendor(payload):
    """
    Vendor cancels an order on their side (out of stock, closed shop, etc).
    On a multi-vendor order this only cancels the reporting vendor's slice —
    the other vendors' fulfillments continue — and releases only that
    vendor's reservation. The order is only marked Cancelled once every
    vendor has cancelled. Uses frappe.db.set_value (not doc.save) so this
    doesn't re-trigger the on_order_updated webhook and echo the
    cancellation back to the vendor that just sent it.
    """
    order_id = payload["order_id"]
    if not frappe.db.exists("Order", order_id):
        frappe.log_error(f"order.cancel: unknown order {order_id}", "Order Cancel")
        return

    from saathimart.api.orders import _find_fulfillment, _recompute_order_status_from_fulfillments
    from saathimart.api.stock import release_reservation

    doc = frappe.get_doc("Order", order_id)
    if doc.status in ("Delivered", "Cancelled"):
        return

    vendor = payload.get("vendor_id") or payload.get("vendor") or doc.vendor
    fulfillment = _find_fulfillment(doc, vendor)

    if fulfillment:
        if fulfillment.status == "Cancelled":
            return
        for item in doc.items:
            if item.vendor == vendor:
                release_reservation(item.vendor, item.product, item.qty)
        frappe.db.set_value("Vendor Fulfillment", fulfillment.name, {
            "status": "Cancelled",
            "notes": f"Cancelled by vendor: {payload.get('reason', '')}",
        })
        doc.reload()
        _recompute_order_status_from_fulfillments(doc)
    else:
        for item in doc.items:
            if item.vendor:
                release_reservation(item.vendor, item.product, item.qty)
        frappe.db.set_value("Order", order_id, {
            "status": "Cancelled",
            "notes": f"Cancelled by vendor: {payload.get('reason', '')}",
        })
        from saathimart.api.notifications import create_order_status_notification
        create_order_status_notification(doc, "Cancelled")


def _apply_stock_receipt(payload):
    """
    Vendor pushes stock.receipt → creates a Stock Ledger Entry of type Receipt.
    payload keys: product_id, qty, source_site, vendor, remarks
    """
    from saathimart.saathimart.doctype.stock_ledger_entry.stock_ledger_entry import make_entry

    product_id = payload.get("product_id")
    if not product_id or not frappe.db.exists("Product", product_id):
        frappe.log_error(title="Stock Receipt", message=f"stock.receipt: unknown product {product_id}")
        return

    qty = frappe.utils.flt(payload.get("qty") or payload.get("stock_qty") or 0)
    if qty == 0:
        return

    make_entry(
        product=product_id,
        qty_change=qty,
        voucher_type="Receipt",
        voucher_no=payload.get("voucher_no") or "VENDOR-PUSH",
        source_site=payload.get("source_site") or "",
        vendor=payload.get("vendor") or "",
        remarks=payload.get("remarks") or "Vendor stock push",
    )


def _apply_price_update(payload):
    """
    Vendor pushes price.update → upsert the Vendor Listing row for that vendor.

    payload keys:
      product_id   — hub Product name
      vendor       — hub Vendor name
      price        — new price (NPR)
      compare_price — optional MRP / compare price
      delivery_zone — optional zone restriction
      sku          — optional vendor SKU
      event_id     — optional UUID, replayed pushes with a seen event_id are
                     idempotent no-ops
      event_seq    — optional per-vendor monotonic sequence (already sent by
                     event_handlers/pricing.py on the vendor side). Guards
                     against applying an update out of order — this matters
                     now that receive()/bulk_receive() defer application to
                     a background job (see _process_inbound_event) instead
                     of applying inline in arrival order, so two pushes for
                     the same listing can reach here in either order. Same
                     pattern as Vendor Stock's last_event_seq guard in
                     api/stock.py._validate_event.
    """
    product_id = payload.get("product_id")
    vendor     = payload.get("vendor")
    new_price  = frappe.utils.flt(payload.get("price") or 0)
    event_id   = payload.get("event_id")
    event_seq  = payload.get("event_seq")

    if not product_id or not vendor or new_price <= 0:
        frappe.log_error(
            title="Price Update",
            message=f"price.update: missing product_id/vendor/price — {payload}",
        )
        return

    if not frappe.db.exists("Product", product_id):
        frappe.log_error(title="Price Update", message=f"price.update: unknown product {product_id}")
        return

    if not frappe.db.exists("Vendor", vendor):
        frappe.log_error(title="Price Update", message=f"price.update: unknown vendor {vendor}")
        return

    delivery_zone = payload.get("delivery_zone") or None
    compare_price = frappe.utils.flt(payload.get("compare_price") or 0)
    sku = payload.get("sku") or ""
    barcode = payload.get("barcode") or ""

    # Find or create Vendor Listing
    filters = {"product": product_id, "vendor": vendor}
    if delivery_zone:
        filters["delivery_zone"] = delivery_zone
    else:
        filters["delivery_zone"] = ["is", "not set"]

    existing = frappe.db.get_value(
        "Vendor Listing",
        filters,
        "name",
    )

    if existing:
        current = frappe.db.get_value(
            "Vendor Listing", existing, ["last_event_seq", "last_event_id"], as_dict=True
        )
        if event_id and current.last_event_id == event_id:
            return  # already applied — vendor retry, not an error
        if event_seq is not None and current.last_event_seq and event_seq <= current.last_event_seq:
            frappe.log_error(
                title="Price Update — Stale Event",
                message=f"price.update: out-of-order event seq={event_seq} <= "
                f"last_seq={current.last_event_seq} for vendor={vendor} "
                f"product={product_id} — ignored",
            )
            return

        doc = frappe.get_doc("Vendor Listing", existing)
        doc.price = new_price
        if compare_price > 0:
            doc.compare_price = compare_price
        if sku:
            doc.sku = sku
        if barcode:
            doc.barcode = barcode
        if delivery_zone:
            doc.delivery_zone = delivery_zone
        doc.status = "Active"
        doc.last_updated = now_datetime()
        doc.last_sync_at = now_datetime()
        if event_id:
            doc.last_event_id = event_id
        if event_seq is not None:
            doc.last_event_seq = event_seq
        doc.save(ignore_permissions=True)
    else:
        doc = frappe.new_doc("Vendor Listing")
        doc.product = product_id
        doc.vendor = vendor
        doc.price = new_price
        doc.compare_price = compare_price
        doc.barcode = barcode
        doc.sku = sku
        doc.delivery_zone = delivery_zone
        doc.status = "Active"
        doc.track_inventory = 1
        doc.allow_backorder = 0
        doc.available_qty = 0
        doc.reserved_qty = 0
        doc.physical_qty = 0
        doc.priority = 0
        doc.estimated_delivery_minutes = 20
        doc.last_updated = now_datetime()
        doc.last_sync_at = now_datetime()
        doc.last_event_id = event_id or ""
        doc.last_event_seq = event_seq or 0
        doc.insert(ignore_permissions=True)


def _apply_barcode_register(payload):
    """
    Vendor tells the hub "I can supply this barcode" — pushed whenever an
    ERPNext Item Barcode is added/updated on the vendor's site (see
    saathimart-vendor/event_handlers/mapping.py::on_item_barcode_change).

    This is what lets on_product_created look up which (usually few, often
    zero) vendors actually carry a new product's barcode instead of
    broadcasting product.new to every vendor site — see
    Vendor Barcode Index and publisher.py::_broadcast_new_product.

    payload keys: vendor, barcode
    """
    vendor = payload.get("vendor")
    barcode = payload.get("barcode")
    if not vendor or not barcode:
        return
    if not frappe.db.exists("Vendor", vendor):
        frappe.log_error(title="Barcode Register", message=f"barcode.register: unknown vendor {vendor}")
        return

    name = f"{vendor}-{barcode}"
    if frappe.db.exists("Vendor Barcode Index", name):
        frappe.db.set_value("Vendor Barcode Index", name, "last_registered", now_datetime())
    else:
        frappe.new_doc("Vendor Barcode Index", vendor=vendor, barcode=barcode,
                        last_registered=now_datetime()).insert(ignore_permissions=True)

    # The product might already exist on the hub — in that case this vendor
    # doesn't need to wait for some *future* product.new broadcast, since
    # there won't be one for a product created in the past. Match right now
    # and notify just this one vendor.
    product = frappe.db.get_value("Product", {"sku": barcode}, ["name", "product_name"], as_dict=True)
    if product:
        from saathimart.events.publisher import _notify_vendor_of_matching_product
        _notify_vendor_of_matching_product(vendor, product.name, barcode, product.product_name)


def _apply_barcode_unregister(payload):
    """
    Vendor removed a barcode from an ERPNext Item (see
    saathimart-vendor/event_handlers/mapping.py::_sync_item_barcodes) —
    remove the matching Vendor Barcode Index row so a future product with
    this barcode doesn't notify a vendor who no longer actually carries it.

    payload keys: vendor, barcode
    """
    vendor = payload.get("vendor")
    barcode = payload.get("barcode")
    if not vendor or not barcode:
        return
    frappe.db.delete("Vendor Barcode Index", {"vendor": vendor, "barcode": barcode})


@frappe.whitelist(allow_guest=True)
def ping():
    """Health check endpoint for hub monitoring and vendor outbox flush."""
    return {"ok": True, "status": "active"}
