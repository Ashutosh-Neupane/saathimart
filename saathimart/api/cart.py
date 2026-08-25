"""
Cart API — session-based cart for guests, user-keyed cart for logged-in users.

Cart identity (ported from saathi_middleware):
  - Guests: the cart keys off a sm_cart_session cookie / explicit session_id.
  - Logged-in users: the cart keys off `user`, so every device and browser
    shares one basket. A pre-login guest cart from this same session is
    adopted (tagged with the user) instead of stranding its items in a
    second, disconnected cart.
"""
import frappe
from frappe import _
from frappe.utils import add_days, flt, now_datetime

from saathimart.api.auth import get_session_id, _set_session_cookie
from saathimart.api.products import select_best_vendor, get_effective_price
from saathimart.api.responses import handle_api_errors
from saathimart.api.utils import guest_rate_limit


def _current_user():
    return frappe.session.user if frappe.session.user != "Guest" else None


def find_active_cart(session_id=None):
    """The Active cart a read-only caller (checkout) should use.

    MUST mirror _get_or_create_cart's resolution order — that function
    resolves a signed-in user's cart by `user` and ignores session_id when
    one exists, so the cart a shopper filled can easily carry a session_id
    that is not the one their browser is sending (they added items on
    another device, or merge_guest_cart folded a guest cart into a user
    cart from an earlier session). checkout() used to look up by session_id
    alone and told those shoppers "Cart not found or already checked out"
    with a full basket on screen.

    Returns the cart docname, or None. Never creates — callers here are
    read-only and a checkout that silently invents an empty cart would be
    worse than the error.
    """
    user = _current_user()

    if user:
        name = frappe.db.get_value("Cart", {"user": user, "status": "Active"}, "name")
        if name:
            return name

    # Falls through for a signed-in shopper with no user-keyed cart yet:
    # their guest cart may still be session-keyed if login ran before any merge.
    if session_id:
        return frappe.db.get_value(
            "Cart", {"session_id": session_id, "status": "Active"}, "name"
        )

    return None


def _get_or_create_cart(session_id=None):
    user = _current_user()

    if user:
        # A signed-in shopper's cart is keyed by user so every device/browser
        # shares one basket.
        name = frappe.db.get_value("Cart", {"user": user, "status": "Active"}, "name")
        if name:
            return frappe.get_doc("Cart", name)
        if session_id:
            # Adopt this device's unowned guest cart so items added before
            # login are not stranded. The ownership guard keeps one customer
            # from adopting another user's active cart via a shared-browser
            # cookie — an unowned-or-own cart is adoptable, anything else
            # falls through to a fresh cart below.
            guest_name = frappe.db.get_value(
                "Cart", {"session_id": session_id, "status": "Active"}, "name"
            )
            if guest_name:
                guest_cart = frappe.get_doc("Cart", guest_name)
                if not guest_cart.user or guest_cart.user == user:
                    guest_cart.user = user
                    guest_cart.save(ignore_permissions=True)
                    return guest_cart
                # Session id belongs to someone else's active cart — drop it so
                # creation generates a fresh unique one instead of colliding.
                session_id = None
    else:
        if not session_id:
            session_id = get_session_id()
        name = frappe.db.get_value(
            "Cart", {"session_id": session_id, "status": "Active"}, "name"
        )
        if name:
            cart = frappe.get_doc("Cart", name)
            _set_session_cookie(session_id)
            return cart

    # session_id is a unique column, but checkout() never deletes the cart it
    # just placed an order from — it only flips status to "CheckedOut" (kept
    # for order history). A browser replaying that stale 30-day cookie would
    # otherwise hit an IntegrityError here. Free the old cart's session_id
    # first so this insert can never collide.
    if session_id:
        stale_name = frappe.db.get_value(
            "Cart", {"session_id": session_id, "status": ["!=", "Active"]}, "name"
        )
        if stale_name:
            frappe.db.set_value(
                "Cart", stale_name, "session_id", f"{session_id}-superseded-{stale_name}"
            )

    cart = frappe.new_doc("Cart")
    # Logged-in users get a generated session_id too, so the cart is tagged to
    # BOTH a stable user and a unique session — what lets it merge with a
    # guest cart later and keeps it off the unique-column collision path.
    cart.session_id = session_id or frappe.generate_hash(length=20)
    cart.user = user
    try:
        cart.source_site = frappe.request.headers.get("X-Source-Site", "")
    except Exception:
        cart.source_site = ""
    cart.expires_at = add_days(now_datetime(), 7)
    cart.insert(ignore_permissions=True)

    if not user:
        _set_session_cookie(cart.session_id)

    return cart


@frappe.whitelist(allow_guest=True)
@handle_api_errors
def set_customer_location(session_id=None, lat=None, lng=None):
    """
    Update the customer's delivery location on the cart.
    Used by the frontend location picker so the backend — not just a client-side
    cookie — is the single source of truth for vendor selection.
    """
    guest_rate_limit("cart.set_location", limit=60, window_seconds=60)
    cart = _get_or_create_cart(session_id)
    cart.customer_lat = flt(lat)
    cart.customer_lng = flt(lng)
    cart.save(ignore_permissions=True)
    return {
        "ok": True,
        "cart_id": cart.name,
        "customer_lat": flt(cart.customer_lat),
        "customer_lng": flt(cart.customer_lng),
    }


def _get_vendor_stock(vendor, product):
    """Get Vendor Stock row for a vendor+product pair."""
    name = f"{vendor}-{product}"
    row = frappe.db.get_value(
        "Vendor Stock", name,
        ["available_qty", "reserved_qty"],
        as_dict=True,
    )
    if not row:
        row = {"available_qty": 0, "reserved_qty": 0}
    row["track_inventory"] = 1
    return row


@frappe.whitelist(allow_guest=True)
@handle_api_errors
def get_cart(session_id=None):
    guest_rate_limit("cart.get", limit=300, window_seconds=60)
    cart = _get_or_create_cart(session_id)
    return cart.as_dict()


@frappe.whitelist(allow_guest=True)
@handle_api_errors
def add_to_cart(session_id=None, product=None, qty=1, vendor=None, delivery_zone=None, customer_lat=None, customer_lng=None):
    guest_rate_limit("cart.add", limit=60, window_seconds=60)
    qty = float(qty)
    if qty <= 0:
        frappe.throw(_("Qty must be positive"))

    product_doc = frappe.get_doc("Product", product)
    if product_doc.status != "Active":
        frappe.throw(_("Product is not available"))
    if product_doc.has_variants:
        frappe.throw(_("This product has multiple options — please select a specific variant"))

    if vendor:
        vl = frappe.get_list(
            "Vendor Listing",
            filters={"product": product, "vendor": vendor, "status": "Active"},
            fields=["name", "price", "track_inventory", "allow_backorder", "delivery_zone"],
            order_by="delivery_zone ASC, price ASC",
        )
        if not vl:
            frappe.throw(_("Vendor listing not found for this product"))
    else:
        cart = _get_or_create_cart(session_id)
        if customer_lat is None and cart.customer_lat is not None:
            customer_lat = flt(cart.customer_lat)
        if customer_lng is None and cart.customer_lng is not None:
            customer_lng = flt(cart.customer_lng)
        best = select_best_vendor(
            product, delivery_zone=delivery_zone,
            customer_lat=customer_lat, customer_lng=customer_lng,
        )
        if not best:
            frappe.throw(_("No vendor available for this product"))
        vendor = best.vendor
        vl = frappe.get_list(
            "Vendor Listing",
            filters={"name": best.name, "status": "Active"},
            fields=["name", "price", "track_inventory", "allow_backorder"],
        )

    rate = flt(vl[0].price)
    track_inventory = vl[0].track_inventory
    allow_backorder = vl[0].allow_backorder

    cart = _get_or_create_cart(session_id)

    if customer_lat is not None:
        cart.customer_lat = flt(customer_lat)
    if customer_lng is not None:
        cart.customer_lng = flt(customer_lng)
    cart.save(ignore_permissions=True)

    # If same product + same vendor already in cart, increment qty
    existing_item = next(
        (item for item in cart.items
         if item.product == product and (item.get("vendor") or None) == vendor),
        None,
    )
    total_qty = qty + (existing_item.qty if existing_item else 0)

    # Server-side availability guard — this vendor's Vendor Stock, not
    # Vendor Listing.available_qty (that column is display-only, refreshed
    # only at product-listing read time; Vendor Stock is the live number).
    # A disabled "Add to Cart" button in a frontend is cosmetic without
    # this: nothing stops a direct API call from adding an unavailable item
    # otherwise, and the only enforcement before this was checkout's
    # atomic_reserve_batch — which still remains the final, race-safe
    # guard for the gap between adding to cart and actually checking out.
    if track_inventory and not allow_backorder:
        stock = _get_vendor_stock(vendor, product)
        available = flt(stock["available_qty"])
        if available <= 0:
            frappe.throw(_("This product is currently out of stock at this vendor"))
        if total_qty > available:
            frappe.throw(_("Only {0} unit(s) available for this product at this vendor").format(available))

    if existing_item:
        existing_item.qty = total_qty
        existing_item.amount = existing_item.qty * existing_item.rate
        cart.save(ignore_permissions=True)
        return cart.as_dict()

    cart.append("items", {
        "product": product,
        "product_name": product_doc.product_name,
        "vendor": vendor,
        "qty": qty,
        "rate": rate,
        "amount": qty * rate,
    })
    cart.save(ignore_permissions=True)
    return cart.as_dict()


@frappe.whitelist(allow_guest=True)
@handle_api_errors
def update_cart_item(session_id=None, product=None, qty=None, vendor=None):
    """
    vendor is optional: add_to_cart() auto-resolves a vendor internally even
    when the caller doesn't pass one, so a caller that only ever added one
    line for this product shouldn't have to already know which vendor got
    picked just to change its qty. Only require vendor when it's genuinely
    ambiguous — the same product sitting in the cart from more than one
    vendor (see test_same_product_different_vendor_are_separate_cart_lines).
    """
    guest_rate_limit("cart.update", limit=60, window_seconds=60)
    qty = float(qty)
    vendor = vendor or None
    cart = _get_or_create_cart(session_id)

    matches = [item for item in cart.items if item.product == product]
    if vendor:
        matches = [item for item in matches if (item.get("vendor") or None) == vendor]
    elif len(matches) > 1:
        frappe.throw(_(
            "This product is in your cart from more than one vendor — specify which one to update"
        ))

    if not matches:
        frappe.throw(_("Item not in cart"))
    item = matches[0]

    if qty <= 0:
        cart.items.remove(item)
    else:
        # Check Vendor Stock only when a vendor is assigned
        if item.vendor:
            stock = _get_vendor_stock(item.vendor, product)
            if stock["track_inventory"] and flt(stock["available_qty"] or 0) < qty:
                frappe.throw(_(
                    "Only {0} unit(s) available."
                ).format(int(stock["available_qty"])))
        item.qty = qty
        item.amount = qty * item.rate

    cart.save(ignore_permissions=True)
    return cart.as_dict()


@frappe.whitelist(allow_guest=True)
@handle_api_errors
def clear_cart(session_id=None):
    guest_rate_limit("cart.clear", limit=30, window_seconds=60)
    cart = _get_or_create_cart(session_id)
    cart.items = []
    cart.save(ignore_permissions=True)
    return {"ok": True}


@frappe.whitelist(allow_guest=True)
@handle_api_errors
def get_cart_summary(session_id=None):
    """
    Lightweight cart summary for the cart badge and mini-cart.
    Returns item count, subtotal, line summaries, and whether this cart
    will split into multiple deliveries at checkout.

    checkout() already returns a full vendor_fulfillments breakdown, but
    only *after* the order is placed and paid for — by then it's too late
    for the customer to have known upfront that a mixed-vendor cart arrives
    as separate deliveries with separate ETAs. add_to_cart() silently
    assigns a vendor per line (see saathimart.api.products.select_best_vendor),
    so a cart can already be multi-vendor without the customer ever having
    been told. This is the earliest point — cart preview, pre-checkout —
    where that can actually be surfaced.
    """
    cart = _get_or_create_cart(session_id)
    items = []
    total_qty = 0
    subtotal = 0.0
    vendors_in_cart = set()
    for item in cart.items:
        if item.vendor:
            vendors_in_cart.add(item.vendor)
        qty = flt(item.qty or 0)
        amount = flt(item.amount or 0)
        total_qty += qty
        subtotal += amount

        # Resolve thumbnail from Product Media / Product
        thumbnail = frappe.db.get_value("Product", item.product, "thumbnail")
        slug = frappe.db.get_value("Product", item.product, "slug")

        # Resolve vendor listing
        vendor_listing = None
        if item.vendor:
            vendor_listing = frappe.db.get_value(
                "Vendor Listing",
                {"product": item.product, "vendor": item.vendor},
                "name",
            )

        items.append({
            "product": item.product,
            "product_name": item.product_name,
            "slug": slug,
            "qty": qty,
            "rate": flt(item.rate or 0),
            "amount": amount,
            "vendor": item.vendor or "",
            "vendor_listing": vendor_listing or "",
            "thumbnail": thumbnail,
        })
    return {
        "cart_id": cart.name,
        "item_count": total_qty,
        "line_count": len(cart.items),
        "subtotal": round(subtotal, 2),
        "items": items,
        "status": cart.status,
        "vendor_count": len(vendors_in_cart),
        "will_split_into_multiple_deliveries": len(vendors_in_cart) > 1,
    }


@frappe.whitelist(allow_guest=True)
@handle_api_errors
def get_cart_count(session_id=None):
    """
    Ultra-lightweight endpoint for the cart badge count.
    Returns just the total item quantity.
    """
    cart = _get_or_create_cart(session_id)
    total_qty = sum(flt(i.qty or 0) for i in cart.items)
    return {"count": int(total_qty)}


def merge_guest_cart(user, guest_session_id):
    """
    Merge a guest cart into the user's logged-in cart after login.
    Called from auth_full.login().

    When no user cart exists yet, the guest cart is adopted in place (tagged
    with the user) rather than duplicated — inserting a second cart carrying
    the guest's session_id while that guest row is still Active violates
    session_id's unique constraint.
    """
    if not guest_session_id:
        try:
            guest_session_id = frappe.request.cookies.get("sm_cart_session")
        except Exception:
            pass
    if not guest_session_id:
        return

    guest_cart_name = frappe.db.get_value(
        "Cart", {"session_id": guest_session_id, "status": "Active"}, "name"
    )
    if not guest_cart_name:
        return

    guest_cart = frappe.get_doc("Cart", guest_cart_name)
    if not guest_cart.items:
        # Nothing to merge, but still bind the empty cart to the user so
        # this device resolves to it instead of starting a disconnected one.
        if not guest_cart.user:
            guest_cart.user = user
            guest_cart.save(ignore_permissions=True)
        return

    user_cart_name = frappe.db.get_value(
        "Cart", {"user": user, "status": "Active"}, "name"
    )
    if user_cart_name:
        user_cart = frappe.get_doc("Cart", user_cart_name)
    else:
        # Adopt the guest cart itself — no second insert, no unique collision.
        guest_cart.user = user
        guest_cart.expires_at = add_days(now_datetime(), 7)
        guest_cart.save(ignore_permissions=True)
        return

    # Merge items: same product + vendor → increment qty, else append
    for g_item in guest_cart.items:
        merged = False
        for u_item in user_cart.items:
            if u_item.product == g_item.product and (u_item.get("vendor") or None) == (g_item.get("vendor") or None):
                u_item.qty += g_item.qty
                u_item.amount = u_item.qty * u_item.rate
                merged = True
                break
        if not merged:
            user_cart.append("items", {
                "product": g_item.product,
                "product_name": g_item.product_name,
                "vendor": g_item.vendor,
                "qty": g_item.qty,
                "rate": g_item.rate,
                "amount": g_item.amount,
            })

    user_cart.save(ignore_permissions=True)
    guest_cart.db_set("status", "Merged")


def expire_abandoned_carts():
    """Scheduled: mark carts abandoned after configured hours and release any reservations."""
    settings = frappe.get_single("Settings")
    hours = settings.abandoned_cart_hours or 24
    frappe.db.sql("""
        UPDATE `tabCart`
        SET status = 'Abandoned'
        WHERE status = 'Active'
          AND modified < DATE_SUB(NOW(), INTERVAL %s HOUR)
    """, hours)

    expired_carts = frappe.db.sql("""
        SELECT name, session_id FROM `tabCart`
        WHERE status = 'Abandoned'
          AND modified < DATE_SUB(NOW(), INTERVAL %s HOUR)
    """, hours, as_dict=True)

    for cart in expired_carts:
        _release_cart_reservations(cart.name)


def _release_cart_reservations(cart_name):
    """Release stock reservations for all items in a cart that was converted to an order."""
    orders = frappe.get_list("Order", filters={"cart_id": cart_name}, fields=["name"])
    for order in orders:
        doc = frappe.get_doc("Order", order.name)
        if doc.payment_status != "Paid" and doc.status not in ("Cancelled", "Delivered"):
            from saathimart.api.stock import release_reservation
            for item in doc.items:
                if item.vendor:
                    release_reservation(item.vendor, item.product, item.qty)
            frappe.db.set_value("Order", order.name, {
                "status": "Cancelled",
                "notes": "Cancelled: cart expired without payment",
            })
