"""
Order API — checkout, status updates, order listing.
Uses calculate_taxes_and_totals for authoritative totals.

Multi-vendor support:
  One customer order can contain items from multiple vendors.
  Each vendor gets a Vendor Fulfillment child row with its own subtotal,
  delivery charge, and status tracking. Stock is reserved per-vendor.
"""
import frappe
from frappe import _
from frappe.utils import flt

from saathimart.api.constants import VALID_ORDER_TRANSITIONS
from saathimart.api.responses import handle_api_errors
from saathimart.api.utils import guest_rate_limit


def _resolve_vendor_listing(product, vendor):
    """Find the Vendor Listing for a product+vendor pair."""
    if not product or not vendor:
        return None
    return frappe.db.get_value(
        "Vendor Listing",
        {"product": product, "vendor": vendor, "status": "Active"},
        "name",
    )


# ── Fulfillment <-> Order status sync ──────────────────────────────────────
# A multi-vendor order carries one Vendor Fulfillment row per vendor, each
# with its own status. These helpers keep the whole-order `status` field
# (used by customer tracking / admin lists) derived from those rows.

_FULFILLMENT_STATUS_ORDER = ["Pending", "Confirmed", "Preparing", "Out for Delivery", "Delivered"]


def _find_fulfillment(doc, vendor):
    """Return the Vendor Fulfillment child row for `vendor` on this Order, if any."""
    if not vendor:
        return None
    for row in (doc.vendor_fulfillments or []):
        if row.vendor == vendor:
            return row
    return None


def _recompute_order_status_from_fulfillments(doc):
    """
    Derive the whole-order status from its Vendor Fulfillment rows: an order
    is only as advanced as its slowest active (non-Cancelled) vendor, and is
    only Cancelled once every vendor has cancelled their part. Persists via
    db.set_value (not doc.save) so this doesn't re-trigger on_order_updated
    and echo the change back to the vendor that just reported it.

    Returns True if the resulting status is "Delivered".
    """
    fulfillments = doc.vendor_fulfillments or []
    if not fulfillments or doc.status == "Refunded":
        return doc.status == "Delivered"

    active = [f for f in fulfillments if f.status != "Cancelled"]
    if not active:
        new_status = "Cancelled"
    else:
        new_status = min(
            active,
            key=lambda f: _FULFILLMENT_STATUS_ORDER.index(f.status)
            if f.status in _FULFILLMENT_STATUS_ORDER else 0,
        ).status

    if new_status != doc.status:
        frappe.db.set_value("Order", doc.name, "status", new_status)
        doc.status = new_status
        from saathimart.api.notifications import create_order_status_notification
        create_order_status_notification(doc, new_status)
    return new_status == "Delivered"


def _cascade_status_to_fulfillments(doc, status):
    """Admin-forced whole-order status override — push it down to every
    fulfillment row that hasn't already reached a terminal state, so the
    per-vendor view stays consistent with the order-level status."""
    for row in (doc.vendor_fulfillments or []):
        if row.status in ("Delivered", "Cancelled"):
            continue
        frappe.db.set_value("Vendor Fulfillment", row.name, "status", status)


@frappe.whitelist(allow_guest=True)
@handle_api_errors
def checkout(session_id, customer_name, customer_phone, delivery_address,
             payment_method="COD", delivery_zone=None, coupon_code=None,
             loyalty_points=0, notes=None, customer_email=None,
             customer_lat=None, customer_lng=None):
    from saathimart.api.utils import check_request_size
    check_request_size()
    """
    Convert an active Cart into a submitted Order.

    Supports multi-vendor carts: items from different vendors are grouped
    into separate Vendor Fulfillment rows, each with its own subtotal.
    Stock is reserved atomically per vendor.
    """
    guest_rate_limit("orders.checkout", limit=20, window_seconds=60)
    from saathimart.api.payments import validate_payment_method
    from saathimart.api.totals import calculate_taxes_and_totals
    from saathimart.saathimart.doctype.coupon.coupon import increment_coupon_usage

    # Resolved the same way add_to_cart resolves it — by user first for a
    # signed-in shopper — so a cart filled on another device, or merged in at
    # login, is still found here. Matching on session_id alone reported
    # "Cart not found" to shoppers looking at a full basket.
    from saathimart.api.cart import find_active_cart

    cart_name = find_active_cart(session_id)
    if not cart_name:
        frappe.throw(_("Cart not found or already checked out"))

    cart = frappe.get_doc("Cart", cart_name)
    if not cart.items:
        frappe.throw(_("Cart is empty"))

    from saathimart.api.cart import _get_customer_location
    customer_lat, customer_lng = _get_customer_location(session_id, customer_lat, customer_lng)

    email = customer_email or (
        frappe.session.user if frappe.session.user != "Guest" else None
    )

    # Validate against the Payment Mode registry and store the canonical
    # mode name, so cron jobs filtering on payment_method="eSewa" keep
    # matching no matter whether the storefront sent a name or a slug.
    payment_method = validate_payment_method(payment_method) or "COD"

    # Group items by vendor for fulfillment splitting
    vendor_groups = {}
    for ci in cart.items:
        v = ci.vendor or ""
        if v not in vendor_groups:
            vendor_groups[v] = []
        vendor_groups[v].append(ci)

    # Multi-vendor carts are supported: one Order, one Vendor Fulfillment
    # row per vendor (see MULTI_VENDOR_ARCHITECTURE.md and the
    # TestCheckoutVendorRouting regression tests). A same-vendor guard
    # here was rejecting exactly the mixed carts the fulfillment model
    # was built to split.
    order = frappe.new_doc("Order")
    order.customer_name    = customer_name
    order.customer_phone   = customer_phone
    order.customer_email   = email or ""
    order.delivery_address = delivery_address
    order.delivery_zone    = delivery_zone
    order.payment_method   = payment_method
    order.coupon_code      = coupon_code or ""
    order.loyalty_points_redeemed = flt(loyalty_points)
    order.notes            = notes or ""
    order.source_site      = cart.source_site or ""
    order.cart_id          = cart.name
    order.vendor            = next(iter(vendor_groups.keys()), "") if vendor_groups else ""
    if customer_lat is not None:
        order.delivery_lat = flt(customer_lat)
    if customer_lng is not None:
        order.delivery_lng = flt(customer_lng)

    for ci in cart.items:
        order.append("items", {
            "product":      ci.product,
            "product_name": ci.product_name,
            "qty":          ci.qty,
            "rate":         ci.rate,
            "vendor":       ci.vendor or "",
            "vendor_listing": _resolve_vendor_listing(ci.product, ci.vendor),
        })

    # Delivery charge from zone
    if delivery_zone:
        zone = frappe.get_doc("Delivery Zone", delivery_zone)
        if zone.is_active:
            subtotal_est = sum(flt(i.qty) * flt(i.rate) for i in cart.items)
            order.delivery_charge = (
                0 if (zone.free_delivery_above and subtotal_est >= zone.free_delivery_above)
                else flt(zone.delivery_charge)
            )

    # Create vendor fulfillments — find nearest warehouse for each vendor
    from saathimart.api.warehouses import find_nearest_warehouse
    for v, items in vendor_groups.items():
        subtotal = sum(flt(i.qty) * flt(i.rate) for i in items)
        # Route to the nearest warehouse with stock for the first product
        # in this vendor's items — ensures we pick a warehouse that can
        # actually fulfill this order.
        first_product = items[0].product if items else None
        nearest_wh = find_nearest_warehouse(v, customer_lat, customer_lng, product=first_product)
        fulfillment = order.append("vendor_fulfillments", {
            "vendor": v,
            "subtotal": subtotal,
            "items_count": len(items),
            "status": "Pending",
            "warehouse": nearest_wh.get("warehouse_name") if nearest_wh else "",
            "warehouse_distance_km": nearest_wh.get("distance_km") if nearest_wh else 0,
        })

    # Run full totals engine
    calculate_taxes_and_totals(order)

    # Reserve vendor stock atomically BEFORE the order exists
    from saathimart.api.stock import atomic_reserve_batch, release_reservation
    reservations = []
    successful_reservations = []
    try:
        for vendor, items in vendor_groups.items():
            for item in items:
                if item.vendor:
                    reservations.append((item.vendor, item.product, item.qty))
        successful_reservations = atomic_reserve_batch(reservations)
    except Exception:
        for vendor, product, qty, success in successful_reservations:
            if success:
                release_reservation(vendor, product, qty)
        raise

    try:
        order.insert(ignore_permissions=True)
    except Exception:
        for vendor, product, qty, success in successful_reservations:
            if success:
                release_reservation(vendor, product, qty)
        raise

    # Mark coupon used — the usage row is what per-customer limits count, so
    # the phone must go with it or max_uses_per_user stays unenforceable.
    if coupon_code:
        try:
            increment_coupon_usage(
                coupon_code,
                order=order.name,
                customer_phone=order.customer_phone,
                customer_email=order.customer_email,
                discount_amount=order.coupon_discount,
            )
        except Exception:
            frappe.log_error(frappe.get_traceback(), f"Coupon usage record failed for {order.name}")

    cart.db_set("status", "CheckedOut")

    if email:
        try:
            from saathimart.api.mailing import send_order_confirmation
            items_summary = [
                {"product_name": i.product_name, "qty": i.qty, "rate": i.rate}
                for i in order.items
            ]
            send_order_confirmation(email, order.name, order.grand_total, items_summary)
        except Exception:
            frappe.log_error(frappe.get_traceback(), "Order confirmation email failed")

    return {
        "order_id":       order.name,
        "vendor":         order.vendor,
        "grand_total":    order.grand_total,
        "coupon_discount": order.coupon_discount,
        "loyalty_discount": order.loyalty_discount,
        "delivery_charge": order.delivery_charge,
        "payment_method": order.payment_method,
        "vendor_fulfillments": [
            {
                "vendor": f.vendor,
                "subtotal": flt(f.subtotal),
                "items_count": f.items_count,
                "status": f.status,
            }
            for f in order.vendor_fulfillments
        ],
    }


@frappe.whitelist()
@handle_api_errors
def get_order(order_id):
    doc = frappe.get_doc("Order", order_id)
    if "SM Admin" not in frappe.get_roles() and doc.customer_email != frappe.session.user:
        frappe.throw(_("Not permitted"), frappe.PermissionError)
    data = doc.as_dict()
    data["status_timeline"] = _build_status_timeline(doc)
    data["items_summary"] = [
        {
            "product": i.product,
            "product_name": i.product_name,
            "qty": flt(i.qty),
            "rate": flt(i.rate),
            "amount": flt(i.amount),
            "vendor": i.vendor or "",
        }
        for i in (doc.items or [])
    ]
    return data


def _build_status_timeline(doc):
    """Build a Blinkit-style status timeline for order tracking."""
    steps = [
        {"status": "Pending",       "label": "Order Placed",     "icon": "fa fa-shopping-cart",  "completed": False},
        {"status": "Confirmed",     "label": "Confirmed",        "icon": "fa fa-check",          "completed": False},
        {"status": "Preparing",     "label": "Preparing",        "icon": "fa fa-box",            "completed": False},
        {"status": "Out for Delivery", "label": "Out for Delivery", "icon": "fa fa-truck",        "completed": False},
        {"status": "Delivered",     "label": "Delivered",        "icon": "fa fa-home",           "completed": False},
    ]
    cancelled = doc.status == "Cancelled"
    refunded = doc.status == "Refunded"

    status_order = ["Pending", "Confirmed", "Preparing", "Out for Delivery", "Delivered"]
    current_idx = -1
    if cancelled:
        current_idx = -2
    elif refunded:
        current_idx = -3
    else:
        try:
            current_idx = status_order.index(doc.status)
        except ValueError:
            current_idx = -1

    for i, step in enumerate(steps):
        if cancelled:
            step["completed"] = False
            step["cancelled"] = True
        elif refunded:
            step["completed"] = False
            step["refunded"] = True
        elif i < current_idx:
            step["completed"] = True
        elif i == current_idx:
            step["completed"] = True
            step["active"] = True
        else:
            step["completed"] = False

    if cancelled:
        steps.append({"status": "Cancelled", "label": "Cancelled", "icon": "fa fa-times-circle", "completed": False, "cancelled": True})
    if refunded:
        steps.append({"status": "Refunded", "label": "Refunded", "icon": "fa fa-undo", "completed": False, "refunded": True})

    return steps


@frappe.whitelist()
@handle_api_errors
def update_order_status(order_id, status):
    allowed = {"SM Admin", "SM Vendor"}
    if not (allowed & set(frappe.get_roles())):
        frappe.throw(_("Not permitted"), frappe.PermissionError)

    doc = frappe.get_doc("Order", order_id)
    if status not in VALID_ORDER_TRANSITIONS.get(doc.status, []):
        frappe.throw(_("Invalid status transition: {0} → {1}").format(doc.status, status))

    doc.status = status
    doc.save(ignore_permissions=True)
    _cascade_status_to_fulfillments(doc, status)

    from saathimart.api.notifications import create_order_status_notification
    create_order_status_notification(doc, status)

    # Finalise or release any per-vendor stock reservation made at checkout.
    # Items with no vendor are the legacy hub-fulfilled path — nothing to do.
    if status == "Cancelled":
        from saathimart.api.stock import release_reservation
        for item in doc.items:
            if item.vendor:
                release_reservation(item.vendor, item.product, item.qty)
    elif status == "Delivered":
        from saathimart.api.stock import confirm_deduction
        for item in doc.items:
            if item.vendor:
                confirm_deduction(item.vendor, item.product, item.qty, order_id=doc.name)

    return {"ok": True, "status": doc.status}


@frappe.whitelist()
@handle_api_errors
def list_orders(status=None, vendor=None, page=1, page_size=20):
    filters = {}
    if status:
        filters["status"] = status
    if vendor:
        filters["vendor"] = vendor
    roles = frappe.get_roles()
    if "SM Vendor" in roles and "SM Admin" not in roles:
        vendor_name = frappe.db.get_value(
            "Vendor", {"contact_email": frappe.session.user}, "name"
        )
        if vendor_name:
            # A vendor's orders include both legacy single-vendor orders
            # (Order.vendor) and multi-vendor orders where they only appear
            # as one of several Vendor Fulfillment rows.
            fulfillment_orders = frappe.db.get_all(
                "Vendor Fulfillment",
                filters={"vendor": vendor_name, "parenttype": "Order"},
                pluck="parent",
            )
            legacy_orders = frappe.db.get_all(
                "Order", filters={"vendor": vendor_name}, pluck="name"
            )
            order_names = list(set(fulfillment_orders) | set(legacy_orders))
            filters["name"] = ["in", order_names] if order_names else ["in", []]
        else:
            filters["name"] = ["in", []]
    elif "SM Customer" in roles and "SM Admin" not in roles:
        email = frappe.session.user
        if email and email != "Guest":
            filters["customer_email"] = email

    page      = max(1, int(page))
    page_size = min(100, max(1, int(page_size)))

    try:
        orders = frappe.get_list(
            "Order",
            filters=filters,
            fields=["name", "customer_name", "status", "payment_status",
                    "grand_total", "creation", "vendor", "payment_method",
                    "delivery_address", "payment_reference"],
            limit_start=(page - 1) * page_size,
            limit=page_size,
            order_by="creation desc",
        )
    except frappe.PermissionError:
        return []

    for o in orders:
        o["item_count"] = frappe.db.count("Order Item", {"parent": o["name"]})

    return orders


@frappe.whitelist(allow_guest=True)
@handle_api_errors
def track_order(order_id, customer_phone=None):
    """
    Public order tracking — mobile-gated.

    The customer's phone number acts as a shared secret: a sequential order
    id alone (ORD-2026-00001) must not be enough to see someone else's name,
    address, and order contents. Logged-in owners/admins don't need this —
    they use get_order.
    """
    guest_rate_limit("orders.track", limit=100, window_seconds=60)
    doc = frappe.get_doc("Order", order_id)

    stored = (doc.customer_phone or "").strip()
    supplied = (customer_phone or "").strip()
    if not stored or not supplied or stored != supplied:
        # Uniform answer for missing/wrong phone and missing order, so the
        # endpoint cannot be used to probe which order ids exist.
        frappe.throw(_("Order not found"), frappe.DoesNotExistError)

    data = doc.as_dict()
    # The secret used to open the door doesn't travel with the payload.
    data.pop("customer_phone", None)
    data["status_timeline"] = _build_status_timeline(doc)
    data["items_summary"] = [
        {
            "product": i.product,
            "product_name": i.product_name,
            "qty": flt(i.qty),
            "rate": flt(i.rate),
            "amount": flt(i.amount),
            "vendor": i.vendor or "",
            "thumbnail": frappe.db.get_value("Product", i.product, "thumbnail"),
        }
        for i in (doc.items or [])
    ]
    return data


@frappe.whitelist()
@handle_api_errors
def refund_order(order_id, amount=None, reason=""):
    doc = frappe.get_doc("Order", order_id)
    if "SM Admin" not in frappe.get_roles():
        frappe.throw(_("Not permitted"), frappe.PermissionError)
    if doc.status == "Cancelled":
        frappe.throw(_("Cannot refund a cancelled order"))
    if doc.payment_status == "Refunded":
        frappe.throw(_("Order already refunded"))

    refund_amount = flt(amount) if amount else flt(doc.grand_total)
    refund_amount = min(refund_amount, flt(doc.grand_total))

    doc.payment_status = "Refunded"
    doc.status = "Refunded"
    doc.notes = (doc.notes or "") + f"\nRefund: NPR {refund_amount} — {reason}".strip()
    doc.save(ignore_permissions=True)

    log = frappe.new_doc("Payment Log")
    log.order = order_id
    log.gateway = doc.payment_method
    log.status = "Refunded"
    log.amount = -refund_amount
    log.reference = doc.payment_reference or ""
    log.transaction_uid = doc.esewa_transaction_uid or ""
    log.notes = reason or "Order refunded"
    log.insert(ignore_permissions=True)
    frappe.db.commit()

    return {"ok": True, "refund_amount": refund_amount}


@frappe.whitelist()
@handle_api_errors
def apply_partial_payment(order_id, amount, gateway, reference="", transaction_uid=""):
    doc = frappe.get_doc("Order", order_id)
    if "SM Admin" not in frappe.get_roles():
        frappe.throw(_("Not permitted"), frappe.PermissionError)
    if doc.payment_status == "Paid":
        frappe.throw(_("Order is already fully paid"))
    if doc.payment_status == "Refunded":
        frappe.throw(_("Cannot apply payment to a refunded order"))

    amount = flt(amount)
    if amount <= 0:
        frappe.throw(_("Payment amount must be positive"))

    existing_paid = flt(doc.grand_total) if doc.payment_status == "Paid" else 0.0
    total_now = existing_paid + amount

    if total_now >= flt(doc.grand_total):
        doc.payment_status = "Paid"
        doc.payment_method = gateway
        doc.payment_reference = reference or doc.payment_reference
        doc.esewa_transaction_uid = transaction_uid or doc.esewa_transaction_uid
        if doc.status == "Pending":
            doc.status = "Confirmed"
    else:
        doc.payment_status = "Partially Paid"

    doc.save(ignore_permissions=True)

    log = frappe.new_doc("Payment Log")
    log.order = order_id
    log.gateway = gateway
    log.status = "Success"
    log.amount = amount
    log.reference = reference
    log.transaction_uid = transaction_uid
    log.insert(ignore_permissions=True)
    frappe.db.commit()

    # Fully-paid via manual/admin application — vendors must hear about it
    # the same way they do for a gateway callback (Payment Entry + unblock
    # prepaid acceptance on their side).
    if doc.payment_status == "Paid":
        try:
            from saathimart.events.publisher import publish_payment_received
            publish_payment_received(order_id, amount=amount, gateway=gateway, reference=reference)
        except Exception:
            frappe.log_error(frappe.get_traceback(), f"payment.received publish failed for {order_id}")

    return {"ok": True, "payment_status": doc.payment_status, "amount_paid": total_now}


def expire_pending_payment_orders():
    """Cron: cancel orders that have been Unpaid beyond the configured expiry hours."""
    settings = frappe.get_single("Settings")
    expiry_hours = getattr(settings, "payment_pending_order_expiry_hours", 24) or 24

    pending = frappe.get_list(
        "Order",
        filters={
            "payment_status": "Unpaid",
            "status": "Pending",
            "creation": ["<", frappe.utils.add_to_date(frappe.utils.now_datetime(), hours=-expiry_hours)],
        },
        fields=["name", "vendor"],
    )
    for row in pending:
        try:
            from saathimart.api.stock import release_reservation
            doc = frappe.get_doc("Order", row.name)
            for item in doc.items:
                if item.vendor:
                    release_reservation(item.vendor, item.product, item.qty)
            frappe.db.set_value("Order", row.name, {
                "status": "Cancelled",
                "notes": "Cancelled: payment not received within expiry window",
            })
            frappe.db.commit()
        except Exception:
            frappe.log_error(frappe.get_traceback(), f"Payment expiry failed for {row.name}")


def retry_failed_order_syncs():
    """Cron: retry order events that failed to deliver to vendors.

    Looks for Dead webhook events related to order operations and retries
    them (up to 5 attempts). Complements dead_letter.retry_dead_letters
    by specifically targeting order-related failures.
    """
    failed_events = frappe.get_list(
        "Webhook Event",
        filters={
            "event_type": ["like", "order.%"],
            "delivery_status": "Dead",
            "retry_count": ["<", 5],
        },
        fields=["name", "event_type", "target_vendor"],
        limit_page_length=20,
    )

    retried = 0
    for evt in failed_events:
        try:
            from saathimart.events.publisher import _enqueue
            frappe.db.set_value("Webhook Event", evt.name, {
                "delivery_status": "Pending",
                "retry_count": (evt.get("retry_count") or 0) + 1,
            })
            frappe.db.commit()
            retried += 1
        except Exception:
            frappe.log_error(frappe.get_traceback(), f"Order sync retry failed for {evt.name}")

    return {"retried": retried, "total_failed": len(failed_events)}
