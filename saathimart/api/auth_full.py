"""
Authentication: signup, login, OTP verification, password reset.

Modeled after trevo_ecommerce patterns but self-contained for
SaathiMart (no ERPNext dependency).
"""
from __future__ import annotations

import hashlib
import secrets

import frappe
from frappe import _
from frappe.utils import add_to_date, now, now_datetime


def _otp(length=6):
    return "".join(secrets.choice("0123456789") for _ in range(length))


def _hash_otp(otp):
    return hashlib.sha256(otp.encode()).hexdigest()


def _rate_limit(key, limit=5, window_seconds=300):
    cache_key = f"sm_rate_limit:{key}"
    current = frappe.cache().get_value(cache_key)
    if current is None:
        frappe.cache().set_value(cache_key, 1, expires_in_sec=window_seconds)
        return True
    if current >= limit:
        frappe.throw(_("Too many attempts. Please try again in a few minutes."))
    frappe.cache().set_value(cache_key, current + 1, expires_in_sec=window_seconds)
    return True


def _should_expose_otp():
    """True when running without real email config (developer mode) — the
    OTP is logged instead of emailed so local/dev signup flows are testable
    without an SMTP setup."""
    return bool(frappe.conf.get("developer_mode"))


def _dispatch_otp_email(email, otp, purpose):
    """Dispatch the OTP email via background worker or inline fallback."""
    if _should_expose_otp():
        frappe.logger().info(f"[dev] OTP for {email} ({purpose}): {otp}")
        return

    try:
        frappe.enqueue(
            "saathimart.api.mailing.send_otp_email",
            email=email,
            otp=otp,
            purpose=purpose,
            now=frappe.flags.in_test or False,
        )
    except Exception:
        try:
            from saathimart.api.mailing import send_otp_email
            send_otp_email(email=email, otp=otp, purpose=purpose)
        except Exception:
            frappe.log_error(
                frappe.get_traceback(),
                f"OTP email dispatch failed for {email} ({purpose})",
            )


def _get_user_token(user):
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


@frappe.whitelist(allow_guest=True)
def signup(email, full_name, contact, password, phone=None):
    """Register a new user with OTP verification."""
    _rate_limit(f"signup:{email}", limit=5, window_seconds=600)
    _rate_limit(f"signup_ip:{frappe.local.request_ip or 'unknown'}", limit=10, window_seconds=600)
    if frappe.db.exists("User", email):
        frappe.throw(_("A user with this email already exists"))

    user = frappe.get_doc({
        "doctype": "User",
        "email": email,
        "first_name": full_name,
        "mobile_no": phone or contact,
        "send_welcome_email": 0,
        "enabled": 0,
    })
    user.append("roles", {"role": "SM Customer"})
    user.insert(ignore_permissions=True)

    otp = _otp(6)
    verification = frappe.get_doc({
        "doctype": "Pending Verification",
        "user": email,
        "otp": _hash_otp(otp),
        "purpose": "signup",
        "expires_at": add_to_date(now(), minutes=15),
    })
    verification.insert(ignore_permissions=True)

    from frappe.utils.password import update_password
    update_password(user=email, pwd=password)

    _dispatch_otp_email(email, otp, purpose="signup")

    return {"message": _("Verification code sent"), "email": email}


@frappe.whitelist(allow_guest=True)
def verify_signup_otp(email, otp):
    """Verify signup OTP and activate the user."""
    _rate_limit(f"verify_otp:{email}", limit=10, window_seconds=600)
    record_name = _validate_otp_record(email, otp, "signup")

    frappe.db.set_value("User", email, "enabled", 1)
    frappe.db.delete("Pending Verification", {"name": record_name})

    frappe.local.login_manager.login_as(email)
    frappe.db.commit()

    token_payload = _get_user_token(email)
    return {
        "message": _("Account verified successfully"),
        "email": email,
        **token_payload,
    }


@frappe.whitelist(allow_guest=True)
def login(usr, pwd, guest_cart_guid=None):
    """Login and optionally merge guest cart."""
    _rate_limit(f"login:{usr}", limit=5, window_seconds=600)
    _rate_limit(f"login_ip:{frappe.local.request_ip or 'unknown'}", limit=15, window_seconds=600)
    try:
        frappe.local.login_manager.authenticate(user=usr, pwd=pwd)
        frappe.local.login_manager.post_login()
    except frappe.AuthenticationError:
        frappe.clear_messages()
        return {"ok": False, "error": _("Incorrect email or password"), "code": 401}
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), f"Login error for {usr}")
        frappe.clear_messages()
        return {"ok": False, "error": _("Incorrect email or password"), "code": 401}

    if guest_cart_guid:
        try:
            from saathimart.api.cart import merge_guest_cart
            merge_guest_cart(usr, guest_cart_guid)
        except Exception as e:
            frappe.log_error(frappe.get_traceback(), f"Guest cart merge failed for {usr}")

    token_payload = _get_user_token(usr)
    user_doc = frappe.get_doc("User", usr)
    return {
        "message": _("Logged in"),
        "user": usr,
        "email": usr,
        "full_name": user_doc.full_name or user_doc.first_name or usr,
        "mobile_no": user_doc.mobile_no or "",
        **token_payload,
    }


@frappe.whitelist(allow_guest=True)
def forgot_password(email):
    """Send password reset OTP."""
    _rate_limit(f"forgot_password:{email}", limit=5, window_seconds=600)
    _rate_limit(f"forgot_password_ip:{frappe.local.request_ip or 'unknown'}", limit=10, window_seconds=600)
    frappe.clear_messages()
    if not frappe.db.exists("User", email):
        return {"message": _("If an account exists, reset instructions have been sent"), "email": email}

    otp = _otp(6)
    verification = frappe.get_doc({
        "doctype": "Pending Verification",
        "user": email,
        "otp": _hash_otp(otp),
        "purpose": "password_reset",
        "expires_at": add_to_date(now(), minutes=15),
    })
    verification.insert(ignore_permissions=True)

    _dispatch_otp_email(email, otp, purpose="password_reset")

    return {"message": _("If an account exists, reset instructions have been sent"), "email": email}


@frappe.whitelist(allow_guest=True)
def verify_forgot_password_otp(email, otp, new_password):
    """Verify OTP and reset password."""
    _rate_limit(f"reset_password:{email}", limit=5, window_seconds=600)
    record_name = _validate_otp_record(email, otp, "password_reset")

    from frappe.utils.password import update_password
    update_password(user=email, pwd=new_password)

    user_doc = frappe.get_doc("User", email)
    if not user_doc.enabled:
        user_doc.enabled = 1
        user_doc.save(ignore_permissions=True)

    frappe.db.delete("Pending Verification", {"name": record_name})

    return {"message": _("Password reset successfully")}


@frappe.whitelist(allow_guest=True)
def resend_otp(email, purpose="signup"):
    """Resend verification OTP."""
    _rate_limit(f"resend_otp:{email}:{purpose}", limit=3, window_seconds=300)
    frappe.clear_messages()
    if not frappe.db.exists("User", email):
        return {"message": _("If an account exists, verification instructions have been sent"), "email": email}

    otp = _otp(6)
    new_expires_at = add_to_date(now(), minutes=15)

    existing = frappe.db.get_value(
        "Pending Verification",
        {"user": email, "purpose": purpose},
        "name",
    )
    if existing:
        frappe.db.set_value("Pending Verification", existing, {"otp": _hash_otp(otp), "expires_at": new_expires_at})
    else:
        frappe.get_doc({
            "doctype": "Pending Verification",
            "user": email,
            "otp": _hash_otp(otp),
            "purpose": purpose,
            "expires_at": new_expires_at,
        }).insert(ignore_permissions=True)

    _dispatch_otp_email(email, otp, purpose=purpose)

    return {"message": _("If an account exists, verification instructions have been sent"), "email": email}


@frappe.whitelist()
def change_password(old_password, new_password):
    """Change password for logged-in user."""
    from frappe.utils.password import check_password, update_password
    user = frappe.session.user
    check_password(user, old_password, delete_tracker_cache=False)
    update_password(user=user, pwd=new_password)
    return {"message": _("Password changed successfully")}


def _validate_otp_record(email, otp, purpose):
    hashed = _hash_otp(otp)
    record = frappe.db.get_value(
        "Pending Verification",
        {"user": email, "purpose": purpose},
        ["name", "expires_at"],
        as_dict=True,
    )
    if not record:
        frappe.throw(_("Invalid or expired verification code"))

    if record.expires_at and record.expires_at < now_datetime():
        frappe.db.delete("Pending Verification", {"name": record.name})
        frappe.throw(_("Invalid or expired verification code"))

    stored_doc = frappe.get_doc("Pending Verification", record.name)
    if stored_doc.otp != hashed:
        frappe.throw(_("Invalid or expired verification code"))

    return record.name