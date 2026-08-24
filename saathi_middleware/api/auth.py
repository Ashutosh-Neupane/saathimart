"""
Auth helpers — permission checks + bootinfo + token generation.
"""
import uuid

import frappe

from saathi_middleware.api.responses import handle_api_errors
from frappe import _


_COOKIE_NAME = "sm_cart_session"


def has_app_permission():
    return bool({"SM Admin", "SM Vendor"} & set(frappe.get_roles()))


def has_cart_permission(doc, ptype):
    if frappe.session.user == "Guest":
        cookie = frappe.request.cookies.get("sm_cart_session") or ""
        return doc.session_id == cookie
    return doc.user == frappe.session.user or "SM Admin" in frappe.get_roles()


def get_session_id():
    """Return the guest session ID.

    Resolution order:
    1. sm_cart_session cookie from the request
    2. session_id in POST body / query string
    3. Generate a new UUID, set it as a cookie, and return it

    Returns None for logged-in users.
    """
    if frappe.session.user and frappe.session.user != "Guest":
        return None

    try:
        existing = frappe.request.cookies.get(_COOKIE_NAME)
        if existing:
            return existing.strip()
    except Exception:
        pass

    try:
        form_val = (
            frappe.form_dict.get("session_id")
            or frappe.request.args.get("session_id")
        )
        if form_val:
            sid = str(form_val).strip()
            _set_session_cookie(sid)
            return sid
    except Exception:
        pass

    new_id = str(uuid.uuid4())
    _set_session_cookie(new_id)
    return new_id


def _set_session_cookie(session_id: str) -> None:
    """Write the sm_cart_session cookie onto the current HTTP response."""
    try:
        frappe.local.cookie_manager.set_cookie(
            _COOKIE_NAME,
            session_id,
            max_age=60 * 60 * 24 * 30,
            httponly=False,
            samesite="None",
            secure=False,
        )
    except Exception:
        pass


def has_order_permission(doc, ptype):
    if {"SM Admin", "SM Vendor"} & set(frappe.get_roles()):
        return True
    return doc.customer_email == frappe.session.user


def has_address_permission(doc, ptype):
    if "SM Admin" in frappe.get_roles():
        return True
    return doc.user == frappe.session.user


def get_address_permission_query_conditions(user=None):
    # has_permission alone only gates single-doc reads/writes, not list
    # queries (frappe.get_list ignores it for filtering) — without this,
    # any logged-in customer could list every other customer's saved
    # addresses via the raw REST endpoint. list_addresses() already scopes
    # itself with an explicit filter, but the raw REST route doesn't go
    # through that function.
    user = user or frappe.session.user
    if "SM Admin" in frappe.get_roles(user):
        return ""
    return f"(`tabSM Address`.user = {frappe.db.escape(user)})"


def has_wishlist_permission(doc, ptype):
    if "SM Admin" in frappe.get_roles():
        return True
    return doc.user == frappe.session.user


def get_wishlist_permission_query_conditions(user=None):
    # Same rationale as get_address_permission_query_conditions — scopes
    # the raw REST list route the way api.wishlist.get_wishlist already
    # scopes itself.
    user = user or frappe.session.user
    if "SM Admin" in frappe.get_roles(user):
        return ""
    return f"(`tabSM Wishlist Item`.user = {frappe.db.escape(user)})"


def has_review_permission(doc, ptype):
    # Governs desk-level single-doc access. Reads are broader than
    # address/wishlist's owner-only pattern: an Approved review is public
    # by design (it's a product review), so any logged-in user may read
    # those; Pending/Rejected ones are only visible to their author or an
    # admin, same as writes.
    if "SM Admin" in frappe.get_roles():
        return True
    if ptype == "read" and doc.status == "Approved":
        return True
    return doc.user == frappe.session.user


def get_review_permission_query_conditions(user=None):
    user = user or frappe.session.user
    if "SM Admin" in frappe.get_roles(user):
        return ""
    return (
        f"(`tabSM Product Review`.status = 'Approved' "
        f"OR `tabSM Product Review`.user = {frappe.db.escape(user)})"
    )


def get_user_token(user):
    doc = frappe.get_doc("User", user)
    if not doc.api_key:
        doc.api_key = frappe.generate_hash(length=15)
        if not doc.get_password("api_secret", raise_exception=False):
            doc.api_secret = frappe.generate_hash(length=15)
        doc.save(ignore_permissions=True)

    api_secret = doc.get_password("api_secret")
    return {
        "api_key": doc.api_key,
        "api_secret": api_secret,
        "token": f"{doc.api_key}:{api_secret}",
    }


def extend_bootinfo(bootinfo):
    currency = "NPR"
    try:
        s = frappe.get_single("System Settings")
        currency = getattr(s, "currency", None) or "NPR"
    except Exception:
        pass

    bootinfo["saathi_middleware"] = {
        "currency":        currency,
        "loyalty_enabled": bool(getattr(frappe.local.conf, "enable_loyalty", 0)),
        "coupons_enabled": bool(getattr(frappe.local.conf, "enable_coupons", 0)),
        "esewa_enabled":   bool(getattr(frappe.local.conf, "enable_esewa", 0)),
        "khalti_enabled":  bool(getattr(frappe.local.conf, "enable_khalti", 0)),
        "sandbox_mode":    bool(getattr(frappe.local.conf, "payment_sandbox_mode", 0)),
    }


@frappe.whitelist()
@handle_api_errors
def get_profile():
    if frappe.session.user == "Guest":
        frappe.throw(_("Not logged in"), frappe.PermissionError)

    user_doc = frappe.get_doc("User", frappe.session.user)
    return {
        "name": user_doc.name,
        "email": user_doc.email,
        "full_name": user_doc.full_name or user_doc.first_name or "",
        "phone": user_doc.mobile_no or "",
        "roles": frappe.get_roles(),
    }


@frappe.whitelist()
@handle_api_errors
def update_profile(full_name=None, phone=None):
    if frappe.session.user == "Guest":
        frappe.throw(_("Not logged in"), frappe.PermissionError)

    user_doc = frappe.get_doc("User", frappe.session.user)
    if full_name:
        user_doc.full_name = full_name
    if phone:
        user_doc.mobile_no = phone
    user_doc.save(ignore_permissions=True)
    return {"ok": True, "message": _("Profile updated")}
