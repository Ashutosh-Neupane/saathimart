"""
Auth helpers — permission checks + bootinfo + token generation.
"""
import frappe
from frappe import _


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
