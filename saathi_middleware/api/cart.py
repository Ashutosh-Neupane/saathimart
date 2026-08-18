"""
Cart API — session-based cart, works for guests and logged-in users.
"""
import frappe
from frappe import _
from frappe.utils import add_days, flt, now_datetime

from saathi_middleware.api.auth import get_session_id, _set_session_cookie


def compose_product_name(item_code, franchise):
    """SM Cart Item.product links to Saathi Item, whose name is the
    composite "{franchise}-{item_code}" — never the bare item_code.

    Callers vary: the storefront's ProductCard/PDP pass their catalog
    `id`, which list_products/get_product already return as the full
    composite slug (catalog.py's `"slug": row["name"]`), plus `vendor`
    as the franchise — so item_code arrives here already prefixed.
    Direct/bare item_code callers (e.g. franchise sync, manual API use)
    still need the prefix added. Without this check, an already-prefixed
    item_code got double-prefixed into a name no Saathi Item has, and
    frappe.get_doc's not-found-vs-no-permission ambiguity surfaced that
    as a misleading "does not have doctype access" error instead of a
    clear "item not found"."""
    if franchise and item_code.startswith(f"{franchise}-"):
        return item_code
    return f"{franchise}-{item_code}" if franchise else item_code


def resolve_item_code(product, franchise):
    """Inverse of compose_product_name: bare item_code for API responses
    and Saathi Order Item.item_code (a plain Data field, not a Link)."""
    if franchise and product.startswith(f"{franchise}-"):
        return product[len(franchise) + 1 :]
    return product


def _item_images(product_names):
    """Batch-fetch Saathi Item.image for a set of cart line products —
    one query instead of one per line, and skips duplicates within a cart."""
    names = list({p for p in product_names if p})
    if not names:
        return {}
    rows = frappe.get_all(
        "Saathi Item", filters={"name": ["in", names]}, fields=["name", "image"]
    )
    return {row.name: row.image for row in rows}


def _check_stock(saathi_item, requested_qty):
    """stock_qty of None means untracked inventory (unlimited) — only a
    numeric value is an actual cap."""
    if saathi_item.stock_qty is not None and requested_qty > saathi_item.stock_qty:
        frappe.throw(
            _("Only {0} of {1} left in stock").format(
                flt(saathi_item.stock_qty), saathi_item.item_name
            )
        )


def _get_or_create_cart(session_id):
    if not session_id:
        session_id = get_session_id()

    user = frappe.session.user if frappe.session.user != "Guest" else None

    # A logged-in user's cart must be the SAME cart on every device/browser
    # they use, not a fresh one per session_id — looking this up by
    # session_id alone (the old behavior) gave every new browser its own
    # disconnected cart, since each carries a different sm_cart_session
    # cookie. For a signed-in user, resolve by `user` first so all of their
    # sessions share one cart; merge_guest_cart (auth_full.py's login) is
    # what folds a pre-login device's guest cart into this one. Guests have
    # no stable identity across devices, so they stay session_id-keyed.
    if user:
        name = frappe.db.get_value("SM Cart", {"user": user, "status": "Active"}, "name")
        if name:
            return frappe.get_doc("SM Cart", name)
    else:
        name = frappe.db.get_value(
            "SM Cart", {"session_id": session_id, "status": "Active"}, "name"
        )
        if name:
            cart = frappe.get_doc("SM Cart", name)
            _set_session_cookie(session_id)
            return cart

    # session_id is unique, but checkout() never deletes the cart it just
    # placed an order from — it only flips status to "CheckedOut" (kept for
    # order history) — so a browser that still carries that same cookie
    # (30-day cart-session cookie; the frontend now rotates it right after
    # a successful checkout, but nothing stops an older tab/bookmark or a
    # non-web caller from replaying a stale one) would otherwise hit a DB
    # IntegrityError here. Free the old cart's session_id first so this
    # insert can never collide with a cart that's done being "Active".
    stale_name = frappe.db.get_value(
        "SM Cart", {"session_id": session_id, "status": ["!=", "Active"]}, "name"
    )
    if stale_name:
        frappe.db.set_value(
            "SM Cart", stale_name, "session_id", f"{session_id}-superseded-{stale_name}"
        )

    cart = frappe.new_doc("SM Cart")
    cart.session_id = session_id
    cart.user = user
    try:
        cart.source_site = frappe.request.headers.get("X-Source-Site", "")
    except Exception:
        cart.source_site = ""
    cart.expires_at = add_days(now_datetime(), 7)
    cart.insert(ignore_permissions=True)

    if not user:
        _set_session_cookie(session_id)

    return cart


@frappe.whitelist(allow_guest=True)
def set_customer_location(session_id, lat, lng):
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


@frappe.whitelist(allow_guest=True)
def get_cart(session_id):
    cart = _get_or_create_cart(session_id)
    return cart.as_dict()


@frappe.whitelist(allow_guest=True)
def add_to_cart(session_id, item_code, qty=1, franchise=None):
    qty = float(qty)
    if qty <= 0:
        frappe.throw(_("Qty must be positive"))

    product = compose_product_name(item_code, franchise)
    saathi_item = frappe.get_doc("Saathi Item", product)
    if not saathi_item.is_active:
        frappe.throw(_("Item is not available"))

    cart = _get_or_create_cart(session_id)
    rate = flt(saathi_item.price)

    for item in cart.items:
        if item.product == product:
            new_qty = item.qty + qty
            _check_stock(saathi_item, new_qty)
            item.qty = new_qty
            item.amount = item.qty * item.rate
            cart.save(ignore_permissions=True)
            return cart.as_dict()

    _check_stock(saathi_item, qty)

    cart.append("items", {
        "product": product,
        "product_name": saathi_item.item_name,
        "franchise": franchise,
        "qty": qty,
        "rate": rate,
        "amount": qty * rate,
    })
    cart.save(ignore_permissions=True)
    return cart.as_dict()


@frappe.whitelist(allow_guest=True)
def update_cart_item(session_id, item_code, qty, franchise=None):
    qty = float(qty)
    cart = _get_or_create_cart(session_id)

    matches = [
        item for item in cart.items
        if item.product == compose_product_name(item_code, item.get("franchise"))
    ]
    if franchise:
        matches = [item for item in matches if (item.get("franchise") or None) == franchise]
    elif len(matches) > 1:
        frappe.throw(_("This product is in your cart from more than one franchise — specify which one to update"))

    if not matches:
        frappe.throw(_("Item not in cart"))
    item = matches[0]

    if qty <= 0:
        cart.items.remove(item)
    else:
        saathi_item = frappe.get_doc("Saathi Item", item.product)
        _check_stock(saathi_item, qty)
        item.qty = qty
        item.amount = qty * item.rate

    cart.save(ignore_permissions=True)
    return cart.as_dict()


@frappe.whitelist(allow_guest=True)
def clear_cart(session_id):
    cart = _get_or_create_cart(session_id)
    cart.items = []
    cart.save(ignore_permissions=True)
    return {"ok": True}


@frappe.whitelist(allow_guest=True)
def get_cart_summary(session_id):
    cart = _get_or_create_cart(session_id)
    images = _item_images([item.product for item in cart.items])
    items = []
    total_qty = 0
    subtotal = 0.0
    for item in cart.items:
        qty = flt(item.qty or 0)
        amount = flt(item.amount or 0)
        total_qty += qty
        subtotal += amount
        items.append({
            "product": resolve_item_code(item.product, item.franchise),
            "product_name": item.product_name,
            "qty": qty,
            "rate": flt(item.rate or 0),
            "amount": amount,
            "franchise": item.franchise or "",
            "image": images.get(item.product) or "",
        })
    return {
        "cart_id": cart.name,
        "item_count": total_qty,
        "line_count": len(cart.items),
        "subtotal": round(subtotal, 2),
        "items": items,
        "status": cart.status,
    }


@frappe.whitelist(allow_guest=True)
def get_cart_count(session_id):
    cart = _get_or_create_cart(session_id)
    total_qty = sum(flt(i.qty or 0) for i in cart.items)
    return {"count": int(total_qty)}


def merge_guest_cart(user, guest_session_id):
    if not guest_session_id:
        try:
            guest_session_id = frappe.request.cookies.get("sm_cart_session")
        except Exception:
            pass
    if not guest_session_id:
        return

    guest_cart_name = frappe.db.get_value(
        "SM Cart", {"session_id": guest_session_id, "status": "Active"}, "name"
    )
    if not guest_cart_name:
        return

    guest_cart = frappe.get_doc("SM Cart", guest_cart_name)
    if not guest_cart.items:
        return

    user_cart_name = frappe.db.get_value(
        "SM Cart", {"user": user, "status": "Active"}, "name"
    )
    if user_cart_name:
        user_cart = frappe.get_doc("SM Cart", user_cart_name)
    else:
        guest_cart.user = user
        guest_cart.expires_at = add_days(now_datetime(), 7)
        guest_cart.save(ignore_permissions=True)
        return

    for g_item in guest_cart.items:
        merged = False
        for u_item in user_cart.items:
            if u_item.product == g_item.product and (u_item.get("franchise") or None) == (g_item.get("franchise") or None):
                u_item.qty += g_item.qty
                u_item.amount = u_item.qty * u_item.rate
                merged = True
                break
        if not merged:
            user_cart.append("items", {
                "product": g_item.product,
                "product_name": g_item.product_name,
                "franchise": g_item.franchise,
                "qty": g_item.qty,
                "rate": g_item.rate,
                "amount": g_item.amount,
            })

    user_cart.save(ignore_permissions=True)
    guest_cart.db_set("status", "Merged")


def expire_abandoned_carts():
    settings = frappe.get_single("Settings")
    hours = getattr(settings, "abandoned_cart_hours", 24) or 24
    frappe.db.sql("""
        UPDATE `tabSM Cart`
        SET status = 'Abandoned'
        WHERE status = 'Active'
        AND modified < DATE_SUB(NOW(), INTERVAL %s HOUR)
    """, [hours])
