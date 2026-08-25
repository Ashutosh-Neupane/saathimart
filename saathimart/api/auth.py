"""
Auth helpers — permission checks + bootinfo + token generation.
"""
import uuid

import frappe
from frappe import _


_COOKIE_NAME = "sm_cart_session"


def get_session_id():
	"""Return the guest cart-session ID (ported from saathi_middleware).

	Resolution order:
	1. sm_cart_session cookie from the request
	2. session_id in POST body / query string
	3. Generate a new UUID, set it as a cookie, and return it

	Returns None for logged-in users — their carts key off `user`, not a
	session, so every device shares one basket.
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


def _set_session_cookie(session_id):
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


def has_app_permission():
    return bool({"SM Admin", "SM Vendor"} & set(frappe.get_roles()))


def has_cart_permission(doc, ptype):
    if frappe.session.user == "Guest":
        return doc.session_id == (frappe.request.cookies.get("sm_cart_session") or "")
    return doc.user == frappe.session.user or "SM Admin" in frappe.get_roles()


def has_order_permission(doc, ptype):
    if {"SM Admin", "SM Vendor"} & set(frappe.get_roles()):
        return True
    return doc.customer_email == frappe.session.user


def has_address_permission(doc, ptype):
    if "SM Admin" in frappe.get_roles():
        return True
    return doc.user == frappe.session.user


def get_user_token(user):
    """Return an api_key:api_secret pair for the user, generating one if absent."""
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
    """Inject saathimart context into every Frappe boot payload."""
    s = frappe.get_single("Settings")
    bootinfo["saathimart"] = {
        "currency":        s.currency or "NPR",
        "loyalty_enabled": bool(getattr(s, "enable_loyalty", 0)),
        "coupons_enabled": bool(getattr(s, "enable_coupons", 0)),
        "esewa_enabled":   bool(getattr(s, "enable_esewa", 0)),
        "sandbox_mode":    bool(getattr(s, "payment_sandbox_mode", 0)),
    }


@frappe.whitelist()
def get_profile():
    """Return the logged-in user's profile data for the frontend."""
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
def update_profile(full_name=None, phone=None):
    """Update the logged-in user's name and phone."""
    if frappe.session.user == "Guest":
        frappe.throw(_("Not logged in"), frappe.PermissionError)

    user_doc = frappe.get_doc("User", frappe.session.user)
    if full_name:
        user_doc.full_name = full_name
    if phone:
        user_doc.mobile_no = phone
    user_doc.save(ignore_permissions=True)
    return {"ok": True, "message": _("Profile updated")}
