"""
Events API — vendor sites poll here, and receive inbound webhook events.
"""
import hashlib
import hmac
import json
import uuid

import frappe
from frappe import _
from frappe.utils import now_datetime, add_to_date

from saathimart.api.constants import VALID_ORDER_TRANSITIONS
from saathimart.api.utils import guest_rate_limit, log_auth_failure


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
    Vendor sites call GET /api/method/saathimart.api.events.poll
    to pull events since a given timestamp.
    Header: X-SM-Secret must match SM Settings.webhook_secret
    """
    guest_rate_limit("events.poll", limit=100, window_seconds=60)
    _verify_secret()

    filters = {"status": "Sent"}
    if since:
        filters["creation"] = [">", since]

    events = frappe.get_list(
        "Webhook Event",
        filters=filters,
        fields=["name", "event_type", "payload", "creation"],
        limit=int(limit),
        order_by="creation asc",
    )
    return {
        "events": [
            {**e, "payload": json.loads(e.payload or "{}")}
            for e in events
        ],
        "polled_at": str(now_datetime()),
    }


@frappe.whitelist(allow_guest=True)
def receive(event=None, payload=None):
    """
    Inbound webhook from vendor sites back to central hub.
    e.g. vendor confirms order, updates stock, pushes price change.
    """
    guest_rate_limit("events.receive", limit=1000, window_seconds=60)
    _verify_timestamp()
    _verify_secret()
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
    doc.status = "Sent"
    doc.insert(ignore_permissions=True)

    _handle_inbound(event, payload or {})
    return {"ok": True}


def _handle_inbound(event, payload):
    if event == "order.confirmed" and payload.get("order_id"):
        _apply_order_status(payload, "Confirmed")

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
        frappe.log_error(f"order status: unknown order {order_id}", "Order Status Update")
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
        frappe.log_error(f"order.delivered: unknown order {order_id}", "Order Delivered")
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


def _apply_stock_receipt(payload):
    """
    Vendor pushes stock.receipt → creates a Stock Ledger Entry of type Receipt.
    payload keys: product_id, qty, source_site, vendor, remarks
    """
    from saathimart.saathimart.doctype.stock_ledger_entry.stock_ledger_entry import make_entry

    product_id = payload.get("product_id")
    if not product_id or not frappe.db.exists("Product", product_id):
        frappe.log_error(f"stock.receipt: unknown product {product_id}", "Stock Receipt")
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
    """
    product_id = payload.get("product_id")
    vendor     = payload.get("vendor")
    new_price  = frappe.utils.flt(payload.get("price") or 0)

    if not product_id or not vendor or new_price <= 0:
        frappe.log_error(
            f"price.update: missing product_id/vendor/price — {payload}",
            "Price Update",
        )
        return

    if not frappe.db.exists("Product", product_id):
        frappe.log_error(f"price.update: unknown product {product_id}", "Price Update")
        return

    if not frappe.db.exists("Vendor", vendor):
        frappe.log_error(f"price.update: unknown vendor {vendor}", "Price Update")
        return

    delivery_zone = payload.get("delivery_zone") or None
    compare_price = frappe.utils.flt(payload.get("compare_price") or 0)
    sku = payload.get("sku") or ""

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
        doc = frappe.get_doc("Vendor Listing", existing)
        doc.price = new_price
        if compare_price > 0:
            doc.compare_price = compare_price
        if sku:
            doc.sku = sku
        if delivery_zone:
            doc.delivery_zone = delivery_zone
        doc.status = "Active"
        doc.last_updated = now_datetime()
        doc.last_sync_at = now_datetime()
        doc.save(ignore_permissions=True)
    else:
        doc = frappe.new_doc("Vendor Listing")
        doc.product = product_id
        doc.vendor = vendor
        doc.price = new_price
        doc.compare_price = compare_price
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
        doc.insert(ignore_permissions=True)


def _verify_secret():
    settings_secret = frappe.get_single("Settings").get_password(
        "webhook_secret", raise_exception=False
    ) or ""
    if not settings_secret:
        frappe.throw(_("Webhook secret not configured"), frappe.AuthenticationError)

    incoming = frappe.request.headers.get("X-SM-Secret", "")
    vendor_id = frappe.request.headers.get("X-Vendor-ID", "")

    if vendor_id:
        vendor = frappe.db.get_value("Vendor", vendor_id, "webhook_secret", as_dict=True)
        if vendor and vendor.webhook_secret:
            expected = vendor.webhook_secret
        else:
            expected = settings_secret
    else:
        expected = settings_secret

    if not hmac.compare_digest(incoming, expected):
        log_auth_failure("webhook", "invalid_secret")
        frappe.throw(_("Invalid secret"), frappe.AuthenticationError)


def _verify_timestamp(max_age_seconds=300):
    """Reject events older than max_age_seconds to prevent replay attacks."""
    ts = frappe.request.headers.get("X-SM-Timestamp")
    if not ts:
        frappe.throw(_("Missing X-SM-Timestamp header"), frappe.AuthenticationError)
    try:
        event_time = float(ts)
    except (TypeError, ValueError):
        frappe.throw(_("Invalid timestamp"), frappe.AuthenticationError)
    now = now_datetime().timestamp()
    if abs(now - event_time) > max_age_seconds:
        frappe.throw(_("Event timestamp too old"), frappe.AuthenticationError)


@frappe.whitelist(allow_guest=True)
def ping():
    """Health check endpoint for hub monitoring and vendor outbox flush."""
    return {"ok": True, "status": "active"}
