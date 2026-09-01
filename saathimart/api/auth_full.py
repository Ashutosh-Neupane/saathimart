"""
Authentication: signup, login, OTP verification, password reset.

Modeled after trevo_ecommerce patterns but self-contained for
SaathiMart (no ERPNext dependency).
"""
from __future__ import annotations

import hashlib
import re
import secrets

import frappe
from frappe import _
from frappe.utils import add_to_date, cint, cstr, now, now_datetime
from saathimart.api.responses import handle_api_errors

# Password policy used by the storefront. The
# composition rules mirror the storefront signup form message for message;
# the zxcvbn grader below is the part the browser cannot run.
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 128
# Frappe grades passwords 1-4 with zxcvbn (System Settings > Minimum Password
# Score). 2 is "Medium" and the framework default — that is the floor here,
# so the guard survives an admin lowering or disabling the framework's own
# policy: signup is allow_guest, and this is the only thing between the
# internet and a new account.
MIN_PASSWORD_SCORE = 2


def _require_password(password, label="Password", email=None, user_inputs=None):
    """Enforce the password policy for any flow that sets a new password.

    Deliberately no special-character requirement: zxcvbn scores real
    strength better than a symbol quota does.
    """
    # Not stripped before the length check — leading/trailing spaces are
    # legitimate password characters, they just cannot be the whole thing.
    if not isinstance(password, str) or not password.strip():
        frappe.throw(_("{0} is required").format(_(label)), frappe.MandatoryError)

    if len(password) < MIN_PASSWORD_LENGTH:
        frappe.throw(
            _("{0} must be at least {1} characters").format(_(label), MIN_PASSWORD_LENGTH),
            frappe.ValidationError,
        )

    if len(password) > MAX_PASSWORD_LENGTH:
        frappe.throw(
            _("{0} must be {1} characters or fewer").format(_(label), MAX_PASSWORD_LENGTH),
            frappe.ValidationError,
        )

    if not re.search(r"[a-zA-Z]", password):
        frappe.throw(
            _("{0} must contain at least one letter").format(_(label)),
            frappe.ValidationError,
        )

    if not re.search(r"[0-9]", password):
        frappe.throw(
            _("{0} must contain at least one number").format(_(label)),
            frappe.ValidationError,
        )

    # Guarded at 3 chars so a short local part like "ab" doesn't ban every
    # password that happens to contain those two letters.
    local_part = cstr(email).split("@")[0].strip().lower()
    if len(local_part) >= 3 and local_part in password.lower():
        frappe.throw(
            _("{0} must not contain your email address").format(_(label)),
            frappe.ValidationError,
        )

    _check_password_strength(password, label, [email] + list(user_inputs or []))

    return password


def _user_inputs_for(email):
    """Personal details zxcvbn should penalise a password for reusing.

    Best-effort: on the reset path the account may not exist (we answer
    those uniformly to avoid leaking whether it does), so a miss is fine —
    the scorer just loses a hint.
    """
    row = frappe.db.get_value(
        "User", email, ["first_name", "last_name", "mobile_no"], as_dict=True
    )
    return [row.first_name, row.last_name, row.mobile_no] if row else []


def _check_password_strength(password, label, user_inputs):
    """Demand at least a Medium score from Frappe's own zxcvbn grader.

    This replaces hand-maintained "common password" lists: zxcvbn already
    knows the leaked-password corpus, keyboard walks (qwerty123), l33t
    substitutions (p@ssw0rd) and dates, scored against the user's own
    details passed in as `user_inputs`.

    The System Settings score is read so an admin who raises it to 3/4 is
    honoured, but floored at MIN_PASSWORD_SCORE. This calls
    frappe.utils.password_strength directly rather than the User doctype
    wrapper, which returns {} when Enable Password Policy is off.
    """
    from frappe.utils.password_strength import test_password_strength

    required = max(
        cint(frappe.get_system_settings("minimum_password_score")),
        MIN_PASSWORD_SCORE,
    )
    result = test_password_strength(password, user_inputs=_tokenize(user_inputs)) or {}
    if cint(result.get("score")) >= required:
        return

    feedback = result.get("feedback") or {}
    # Assembled as plain text on purpose: Frappe's own handle_password_test_fail
    # emits HTML, which the storefront would render as literal markup.
    parts = [cstr(feedback.get("warning"))]
    parts.extend(cstr(suggestion) for suggestion in (feedback.get("suggestions") or []))
    hint = " ".join(part.strip() for part in parts if part.strip())
    message = _("{0} is too weak.").format(_(label))
    frappe.throw(f"{message} {hint}".strip(), frappe.ValidationError)


def _tokenize(values):
    """Expand personal details into the word list zxcvbn actually matches on.

    zxcvbn treats each user_input as one whole lowercased dictionary entry, so
    a full name arrives as the single term "bibek karki" and does nothing to
    flag "BibekKarki1". Splitting on non-word characters gives it "bibek" and
    "karki" separately. The 3-char floor keeps initials and "of"/"ko" style
    fragments from blacklisting half the dictionary.
    """
    tokens = []
    for value in values:
        text = cstr(value).strip()
        if not text:
            continue
        tokens.append(text)
        tokens.extend(part for part in re.split(r"[^\w]+", text) if len(part) >= 3)
    return tokens


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
@handle_api_errors
def signup(email, full_name, contact, password, phone=None):
    """Register a new user with OTP verification."""
    _rate_limit(f"signup:{email}", limit=5, window_seconds=600)
    _rate_limit(f"signup_ip:{frappe.local.request_ip or 'unknown'}", limit=10, window_seconds=600)

    # Validate everything the request carries before anything touches the
    # database — signup is allow_guest, so this endpoint is reachable by curl
    # with no form in between.
    _require_password(
        password,
        "Password",
        email=email,
        user_inputs=[full_name, contact, phone],
    )

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
@handle_api_errors
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
@handle_api_errors
def login(usr, pwd, guest_cart_guid=None):
    """Login and optionally merge guest cart."""
    _rate_limit(f"login:{usr}", limit=5, window_seconds=600)
    _rate_limit(f"login_ip:{frappe.local.request_ip or 'unknown'}", limit=15, window_seconds=600)
    try:
        frappe.local.login_manager.authenticate(user=usr, pwd=pwd)
        frappe.local.login_manager.post_login()
    except frappe.AuthenticationError:
        # Returned, not thrown, and kept at HTTP 200: NextAuth's authorize()
        # treats a rejected call as a crash rather than a bad password. The
        # error_code matches what the after_request hook emits for thrown
        # AuthenticationErrors so both paths classify identically.
        from saathimart.api.responses import UNAUTHORIZED, error_response
        frappe.clear_messages()
        return error_response(_("Incorrect email or password"), UNAUTHORIZED)
    except Exception:
        from saathimart.api.responses import UNAUTHORIZED, error_response
        frappe.log_error(frappe.get_traceback(), f"Login error for {usr}")
        frappe.clear_messages()
        return error_response(_("Incorrect email or password"), UNAUTHORIZED)

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
@handle_api_errors
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
@handle_api_errors
def verify_forgot_password_otp(email, otp, new_password):
    """Verify OTP and reset password."""
    _rate_limit(f"reset_password:{email}", limit=5, window_seconds=600)
    _require_password(new_password, "New Password", email=email,
                      user_inputs=_user_inputs_for(email))
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
@handle_api_errors
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
@handle_api_errors
def change_password(old_password, new_password):
    """Change password for logged-in user."""
    from frappe.utils.password import check_password, update_password
    user = frappe.session.user
    _require_password(new_password, "New Password", email=user,
                      user_inputs=_user_inputs_for(user))
    check_password(user, old_password, delete_tracker_cache=False)
    update_password(user=user, pwd=new_password)
    return {"message": _("Password changed successfully")}


@frappe.whitelist()
@handle_api_errors
def cleanup_expired_verifications():
    """Purge expired OTP rows. Daily cron — Pending Verification is written on
    every signup/reset attempt and deleted only when consumed, so without this
    the table grows one abandoned row per uncompleted attempt forever."""
    expired = frappe.get_all(
        "Pending Verification",
        filters={"expires_at": ["<", now_datetime()]},
        pluck="name",
        limit=500,
    )
    if expired:
        frappe.db.delete("Pending Verification", {"name": ["in", expired]})
        frappe.db.commit()
    return {"deleted": len(expired)}


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
