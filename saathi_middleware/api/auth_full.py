"""
Authentication: signup, login, OTP verification, password reset.

Modeled after saathimart patterns but self-contained for
Saathi Middleware.
"""
from __future__ import annotations

import hashlib
import re
import secrets

import frappe
from frappe import _
from frappe.utils import add_to_date, cint, cstr, now, now_datetime, validate_email_address

MIN_PASSWORD_LENGTH = 8
# Capped because hashing burns our CPU, not the caller's: on a whitelisted
# allow_guest endpoint an unbounded password is a cheap way to make the
# server do arbitrary work per request.
MAX_PASSWORD_LENGTH = 128

# Frappe grades passwords 1-4 with zxcvbn (System Settings > Minimum Password
# Score). 2 is "Medium" and the framework default — that is the floor here.
MIN_PASSWORD_SCORE = 2


def _require(value, label):
    """Reject a missing or blank field before it can reach the database.

    Whitelisted args come straight off the request body: an omitted key
    arrives as None and an empty form field as "", and both are falsy but
    neither raises on its own. Without this, signup happily wrote a User
    row for input it should have refused.
    """
    value = cstr(value).strip()
    if not value:
        frappe.throw(_("{0} is required").format(_(label)), frappe.MandatoryError)
    return value


def _require_password(password, label="Password", email=None, user_inputs=None):
    """Enforce the password policy.

    The composition rules below mirror `passwordFieldSchema` in saathimart-fe's
    lib/validations/auth.ts message for message, so the form and the API agree.
    On top of those, `_check_password_strength` applies Frappe's zxcvbn grader,
    which the browser cannot run — a weak-but-well-formed password is caught
    here rather than in the form.

    This is the rule that actually holds: signup is whitelisted with
    allow_guest, so curl, Postman and any mobile client reach it without ever
    loading the form.

    Deliberately no special-character requirement: the frontend calls that
    "pragmatic for Nepal UX", and zxcvbn scores real strength better than a
    symbol quota does.
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

    This is what replaces hand-maintained "common password" lists: zxcvbn
    already knows the leaked-password corpus, keyboard walks (qwerty123),
    l33t substitutions (p@ssw0rd) and dates, and it scores them against the
    user's own details passed in as `user_inputs`.

    The System Settings score is read so an admin who raises it to 3/4 is
    honoured, but floored at Medium so the guard survives the score being
    lowered or Enable Password Policy being switched off — signup is
    allow_guest, and this is the only thing between the internet and a new
    account. Note this calls frappe.utils.password_strength directly rather
    than the User doctype wrapper, which returns {} when that toggle is off.
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
    "karki" separately.

    Measured effect: "BibekKarki1" scores 4 unsplit and 3 split. Real, but note
    it still clears the Medium (2) floor — at Medium this mainly matters for
    passwords already near the line, and it starts hard-blocking only if
    Minimum Password Score is raised to 4. The explicit email-containment check
    above is what reliably stops the blatant self-referential case.
    """
    tokens = []
    for value in values:
        text = cstr(value).strip()
        if not text:
            continue
        tokens.append(text)
        # 3-char floor keeps initials and "of"/"ko" style fragments from
        # blacklisting half the dictionary.
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
    return bool(frappe.conf.get("developer_mode"))


def _dispatch_otp_email(email, otp, purpose):
    if _should_expose_otp():
        message = f"[dev] OTP for {email} ({purpose}): {otp}"
        # frappe.logger()'s default level is ERROR under gunicorn (it only
        # relaxes to WARNING under frappe._dev_server, i.e. `bench serve`) —
        # developer_mode alone doesn't make .info() visible in `docker logs`,
        # so print() too since PYTHONUNBUFFERED=1 guarantees it reaches stdout.
        frappe.logger().info(message)
        print(message)
        return

    try:
        # now=True: run synchronously in this request instead of queueing
        # for the background worker to pick up later — an OTP the shopper
        # is actively waiting for should never sit in a job queue.
        frappe.enqueue(
            "saathi_middleware.api.mailing.send_otp_email",
            email=email,
            otp=otp,
            purpose=purpose,
            now=True,
        )
    except Exception:
        try:
            from saathi_middleware.api.mailing import send_otp_email
            send_otp_email(email=email, otp=otp, purpose=purpose)
        except Exception:
            frappe.log_error(
                frappe.get_traceback(),
                f"OTP email dispatch failed for {email} ({purpose})",
            )


def _get_user_token(user):
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
def signup(email=None, full_name=None, contact=None, password=None, phone=None):
    # Defaulted to None deliberately: a key missing from the request body
    # would otherwise raise TypeError and surface as a 500 instead of a
    # readable "Password is required".
    email = _require(email, "Email").lower()
    if not validate_email_address(email):
        frappe.throw(_("Please enter a valid email address"), frappe.ValidationError)
    full_name = _require(full_name, "Full name")
    contact = _require(contact, "Contact")
    password = _require_password(
        password, email=email, user_inputs=[full_name, contact, phone]
    )

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
        "doctype": "SM Pending Verification",
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
    _rate_limit(f"verify_otp:{email}", limit=10, window_seconds=600)
    record_name = _validate_otp_record(email, otp, "signup")

    frappe.db.set_value("User", email, "enabled", 1)
    frappe.db.delete("SM Pending Verification", {"name": record_name})

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
    _rate_limit(f"login:{usr}", limit=5, window_seconds=600)
    _rate_limit(f"login_ip:{frappe.local.request_ip or 'unknown'}", limit=15, window_seconds=600)
    try:
        frappe.local.login_manager.authenticate(user=usr, pwd=pwd)
        frappe.local.login_manager.post_login()
    except frappe.AuthenticationError:
        frappe.clear_messages()
        return {"ok": False, "error": _("Incorrect email or password"), "code": 401}
    except Exception:
        frappe.log_error(frappe.get_traceback(), f"Login error for {usr}")
        frappe.clear_messages()
        return {"ok": False, "error": _("Incorrect email or password"), "code": 401}

    if guest_cart_guid:
        try:
            from saathi_middleware.api.cart import merge_guest_cart
            merge_guest_cart(usr, guest_cart_guid)
        except Exception:
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
    _rate_limit(f"forgot_password:{email}", limit=5, window_seconds=600)
    _rate_limit(f"forgot_password_ip:{frappe.local.request_ip or 'unknown'}", limit=10, window_seconds=600)
    frappe.clear_messages()
    if not frappe.db.exists("User", email):
        return {"message": _("If an account exists, reset instructions have been sent"), "email": email}

    otp = _otp(6)
    verification = frappe.get_doc({
        "doctype": "SM Pending Verification",
        "user": email,
        "otp": _hash_otp(otp),
        "purpose": "password_reset",
        "expires_at": add_to_date(now(), minutes=15),
    })
    verification.insert(ignore_permissions=True)

    _dispatch_otp_email(email, otp, purpose="password_reset")

    return {"message": _("If an account exists, reset instructions have been sent"), "email": email}


@frappe.whitelist(allow_guest=True)
def verify_forgot_password_otp(email=None, otp=None, new_password=None):
    email = _require(email, "Email").lower()
    otp = _require(otp, "OTP")
    new_password = _require_password(
        new_password, "New password", email=email, user_inputs=_user_inputs_for(email)
    )

    _rate_limit(f"reset_password:{email}", limit=5, window_seconds=600)
    record_name = _validate_otp_record(email, otp, "password_reset")

    from frappe.utils.password import update_password
    update_password(user=email, pwd=new_password)

    user_doc = frappe.get_doc("User", email)
    if not user_doc.enabled:
        user_doc.enabled = 1
        user_doc.save(ignore_permissions=True)

    frappe.db.delete("SM Pending Verification", {"name": record_name})

    return {"message": _("Password reset successfully")}


@frappe.whitelist(allow_guest=True)
def resend_otp(email, purpose="signup"):
    _rate_limit(f"resend_otp:{email}:{purpose}", limit=3, window_seconds=300)
    frappe.clear_messages()
    if not frappe.db.exists("User", email):
        return {"message": _("If an account exists, verification instructions have been sent"), "email": email}

    otp = _otp(6)
    new_expires_at = add_to_date(now(), minutes=15)

    existing = frappe.db.get_value(
        "SM Pending Verification",
        {"user": email, "purpose": purpose},
        "name",
    )
    if existing:
        frappe.db.set_value("SM Pending Verification", existing, {"otp": _hash_otp(otp), "expires_at": new_expires_at})
    else:
        frappe.get_doc({
            "doctype": "SM Pending Verification",
            "user": email,
            "otp": _hash_otp(otp),
            "purpose": purpose,
            "expires_at": new_expires_at,
        }).insert(ignore_permissions=True)

    _dispatch_otp_email(email, otp, purpose=purpose)

    return {"message": _("If an account exists, verification instructions have been sent"), "email": email}


@frappe.whitelist()
def change_password(old_password=None, new_password=None):
    user = frappe.session.user
    old_password = _require(old_password, "Current password")
    new_password = _require_password(
        new_password, "New password", email=user, user_inputs=_user_inputs_for(user)
    )
    if new_password == old_password:
        frappe.throw(
            _("New password must be different from your current password"),
            frappe.ValidationError,
        )

    from frappe.utils.password import check_password, update_password
    check_password(user, old_password, delete_tracker_cache=False)
    update_password(user=user, pwd=new_password)
    return {"message": _("Password changed successfully")}


def _validate_otp_record(email, otp, purpose):
    hashed = _hash_otp(otp)
    record = frappe.db.get_value(
        "SM Pending Verification",
        {"user": email, "purpose": purpose},
        ["name", "expires_at"],
        as_dict=True,
    )
    if not record:
        frappe.throw(_("Invalid or expired verification code"))

    if record.expires_at and record.expires_at < now_datetime():
        frappe.db.delete("SM Pending Verification", {"name": record.name})
        frappe.throw(_("Invalid or expired verification code"))

    stored_doc = frappe.get_doc("SM Pending Verification", record.name)
    if stored_doc.otp != hashed:
        frappe.throw(_("Invalid or expired verification code"))

    return record.name


@frappe.whitelist()
def cleanup_expired_verifications():
    expired = frappe.get_all(
        "SM Pending Verification",
        filters={"expires_at": ["<", now_datetime()]},
        pluck="name",
        limit=500,
    )
    if expired:
        frappe.db.delete("SM Pending Verification", {"name": ["in", expired]})
        frappe.db.commit()
    return {"deleted": len(expired)}
