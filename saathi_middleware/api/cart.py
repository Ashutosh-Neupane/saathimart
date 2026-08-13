"""
Cart API — session-based cart, works for guests and logged-in users.
"""
import frappe
from frappe import _
from frappe.utils import add_days, flt, now_datetime


def compose_product_name(item_code, franchise):
    """SM Cart Item.product links to Saathi Item, whose name is the
    composite "{franchise}-{item_code}" — never the bare item_code."""
    return f"{franchise}-{item_code}" if franchise else item_code


def resolve_item_code(product, franchise):
    """Inverse of compose_product_name: bare item_code for API responses
    and Saathi Order Item.item_code (a plain Data field, not a Link)."""
    if franchise and product.startswith(f"{franchise}-"):
        return product[len(franchise) + 1 :]
    return product


def _get_or_create_cart(session_id):
    name = frappe.db.get_value(
        "SM Cart", {"session_id": session_id, "status": "Active"}, "name"
    )
    if name:
        return frappe.get_doc("SM Cart", name)

    cart = frappe.new_doc("SM Cart")
    cart.session_id = session_id
    cart.user = frappe.session.user if frappe.session.user != "Guest" else None
    try:
        cart.source_site = frappe.request.headers.get("X-Source-Site", "")
    except Exception:
        cart.source_site = ""
    cart.expires_at = add_days(now_datetime(), 7)
    cart.insert(ignore_permissions=True)
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
            item.qty += qty
            item.amount = item.qty * item.rate
            cart.save(ignore_permissions=True)
            return cart.as_dict()

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
        user_cart = frappe.new_doc("SM Cart")
        user_cart.user = user
        user_cart.session_id = guest_session_id
        user_cart.expires_at = add_days(now_datetime(), 7)
        user_cart.insert(ignore_permissions=True)

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
    """)
