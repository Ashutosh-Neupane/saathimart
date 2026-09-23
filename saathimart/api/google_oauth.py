"""Google Sign-In ("Sign in with Google") for the storefront.

Flow: the Next.js app runs the OAuth dance with Google (NextAuth's Google
provider) and POSTS the resulting ID token here. We verify the token
directly against Google — never trusting the FE, which is untrusted client
code — then issue the exact same token payload the password login returns,
so the FE session layer treats both flows identically.

Account model mirrors signup: Google verified the email (we hard-require
``email_verified``), which is the same ownership guarantee our OTP flow
gives, so a Google login may activate a pending account or auto-provision
a new one. Existing password accounts with the same email are linked by
virtue of sharing the User record — one identity per email, per Nepal
ecommerce norms (single customer ledger).

Verification uses Google's tokeninfo endpoint rather than a local JWT
library: it needs zero new dependencies (requests is already pinned) and
stays correct automatically (Google rotates signing keys without us
republishing the app). Rate-limit the endpoint hard — it is allow_guest.
"""
from __future__ import annotations

import frappe
import requests
from frappe import _
from frappe.utils import cint, now_datetime
from saathimart.api.auth import get_user_token
from saathimart.api.auth_full import _rate_limit
from saathimart.api.responses import handle_api_errors

TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"
# Per Google's spec the issuer is either the bare host or the https URL.
GOOGLE_ISSUERS = ("accounts.google.com", "https://accounts.google.com")
# ID tokens are ~1-2 KB; cap input before it ever reaches requests.
MAX_CREDENTIAL_LENGTH = 4096


def _allowed_client_ids():
    """Client IDs allowed to mint tokens for this hub (admin-managed)."""
    raw = frappe.db.get_single_value(
        "SaathiMart Settings", "google_client_id_allowlist"
    ) or ""
    return [c.strip() for c in raw.replace(",", "\n").split() if c.strip()]


def _verify_id_token(credential):
    """Validate the ID token end-to-end; return (claims, email)."""
    if not credential or not isinstance(credential, str):
        frappe.throw(_("Invalid Google credential"))
    if len(credential) > MAX_CREDENTIAL_LENGTH:
        frappe.throw(_("Invalid Google credential"))

    try:
        resp = requests.get(
            TOKENINFO_URL, params={"id_token": credential}, timeout=10
        )
        resp.raise_for_status()
        claims = resp.json()
    except Exception:
        frappe.log_error(
            frappe.get_traceback(), "Google tokeninfo verification failed"
        )
        frappe.throw(_("Could not verify the Google sign-in. Please try again."))

    allowed = _allowed_client_ids()
    if not allowed:
        frappe.throw(_(
            "Google sign-in is not configured. "
            "Add client IDs under SaathiMart Settings."
        ))
    if claims.get("aud") not in allowed:
        frappe.throw(_("Google sign-in was issued for an unknown application"))
    if claims.get("iss") not in GOOGLE_ISSUERS:
        frappe.throw(_("Google sign-in has an invalid issuer"))

    try:
        exp = int(claims.get("exp") or 0)
    except (TypeError, ValueError):
        exp = 0
    if not exp or exp < now_datetime().timestamp():
        frappe.throw(_("Google sign-in has expired. Please try again."))

    email = (claims.get("email") or "").strip().lower()
    # email_verified is what makes auto-provisioning safe: Google has
    # proven the signer controls this mailbox, the same guarantee the
    # signup OTP flow gives before enabling an account.
    if not email or not cint(claims.get("email_verified") or 0):
        frappe.throw(_("Your Google account does not share a verified email"))

    return claims, email


def _record_social_login(email, claims):
    """Mirror the linkage into Social Login Key so Desk shows it."""
    provider = "google"
    userid = claims.get("sub") or ""
    if not userid:
        return
    if frappe.db.exists(
        "Social Login Key",
        {"social_login_provider": provider, "userid": userid},
    ):
        return
    try:
        frappe.get_doc(
            {
                "doctype": "Social Login Key",
                "social_login_provider": provider,
                "userid": userid,
                "user": email,
                "username": claims.get("name") or email,
                "email": email,
            }
        ).insert(ignore_permissions=True)
    except Exception:
        # Informational only — never block a login on the mirror write.
        frappe.log_error(
            frappe.get_traceback(), f"Social Login Key mirror failed for {email}"
        )


@frappe.whitelist(allow_guest=True)
@handle_api_errors
def google_oauth_login(credential=None, guest_cart_guid=None):
    """Sign in (or auto-signup) with a Google ID token.

    Returns the same payload shape as auth_full.login — the FE session
    layer consumes both identically.
    """
    _rate_limit(
        f"google_ip:{frappe.local.request_ip or 'unknown'}",
        limit=20,
        window_seconds=600,
    )

    claims, email = _verify_id_token(credential)

    created = False
    if frappe.db.exists("User", email):
        user = frappe.get_doc("User", email)
        if not user.enabled:
            # Pending-OTP account whose owner just proved mailbox control
            # via Google — activate it, same as verify_signup_otp would.
            user.enabled = 1
            user.save(ignore_permissions=True)
    else:
        user = frappe.get_doc(
            {
                "doctype": "User",
                "email": email,
                "first_name": claims.get("name") or email.split("@")[0],
                "send_welcome_email": 0,
                "enabled": 1,
            }
        )
        user.append("roles", {"role": "SM Customer"})
        user.insert(ignore_permissions=True)
        created = True

    _record_social_login(email, claims)

    if guest_cart_guid:
        try:
            from saathimart.api.cart import merge_guest_cart

            merge_guest_cart(email, guest_cart_guid)
        except Exception:
            frappe.log_error(
                frappe.get_traceback(), f"Guest cart merge failed for {email}"
            )

    frappe.local.login_manager.login_as(email)
    frappe.db.commit()

    token_payload = get_user_token(email)
    user_doc = frappe.get_doc("User", email)
    return {
        "message": _("Signed in with Google"),
        "created": created,
        "user": email,
        "email": email,
        "full_name": user_doc.full_name or email,
        "mobile_no": user_doc.mobile_no or "",
        **token_payload,
    }
