"""
Shared utilities for SaathiMart hub API endpoints.

Single source of truth for:
- Rate limiting
- Request logging
- Common validation helpers
- Vendor->hub push authentication
"""
import hashlib
import hmac
import json
from datetime import datetime, timezone

import frappe
from frappe import _
from frappe.utils import now_datetime


def safe_enqueue(*args, **kwargs):
    """
    frappe.enqueue(), but never lets a background-job scheduling failure
    break the caller. frappe.enqueue() itself can raise QueueOverloaded
    (Frappe's own cap on pending RQ jobs) when nothing is draining the
    queue fast enough — several call sites here run synchronously inside a
    request a customer or vendor is waiting on, so an uncaught
    QueueOverloaded wouldn't just skip an optimization, it would fail the
    whole request. Shared here (rather than duplicated per-module) so
    saathimart.events.publisher and saathimart.api.events both get the
    same protection from one place — same pattern as
    saathimart_vendor.utils.safe_enqueue on the vendor side.
    """
    try:
        frappe.enqueue(*args, **kwargs)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Background job scheduling failed")


def rate_limit(key, limit=10, window_seconds=60):
    """
    Rate limit by a cache key (e.g. IP or vendor ID).
    Raises frappe.ValidationError when limit is exceeded.
    """
    cache_key = f"sm_rate_limit:{key}"
    current = frappe.cache().get_value(cache_key)
    if current is None:
        frappe.cache().set_value(cache_key, 1, expires_in_sec=window_seconds)
        return True
    if current >= limit:
        frappe.throw(_("Rate limit exceeded. Please try again later."))
    frappe.cache().set_value(cache_key, current + 1, expires_in_sec=window_seconds)
    return True


def guest_rate_limit(endpoint, limit=60, window_seconds=60):
    """
    Rate limit a guest endpoint by client IP.
    Falls back to 'unknown' if IP cannot be determined.
    """
    try:
        ip = frappe.get_request_header("X-Forwarded-For", "").split(",")[0].strip()
        if not ip:
            ip = frappe.get_request_header("X-Real-IP", "")
        if not ip and frappe.request:
            ip = getattr(frappe.request, "ip", "unknown") or "unknown"
    except Exception:
        ip = "unknown"
    return rate_limit(f"{endpoint}:{ip}", limit=limit, window_seconds=window_seconds)


def compute_hmac_signature(secret, timestamp, body):
    """
    Stripe-style request signature: HMAC-SHA256 over "<timestamp>.<body>"
    keyed with the shared webhook secret. The secret itself never crosses
    the wire — a captured request reveals nothing reusable.
    """
    msg = f"{timestamp}.".encode() + (body if isinstance(body, bytes) else body.encode())
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


def verify_hub_secret(endpoint):
    """
    Authenticate an inbound vendor push.

    Required: X-SM-Signature — HMAC-SHA256(shared_secret, "<ts>.<raw_body>")
    alongside X-SM-Timestamp. The secret never travels, so a leaked header
    or logged request cannot be replayed into a valid credential.

    Requests without a valid HMAC signature are rejected. The legacy bare
    X-SM-Secret header fallback has been removed.

    No-ops when there is no active HTTP request — i.e. when the caller is
    invoked internally after the true entry point (events.receive) already
    authenticated the request, or called directly in tests. A real inbound
    HTTP call always has frappe.request set by the time a whitelisted method
    runs, so this guard never opens a bypass for actual traffic.
    """
    if not frappe.request:
        return

    # Rate-limit auth failures per IP to block brute-force attacks
    from saathimart.api.rate_limiter import check_rate_limit, record_failure, clear_failures
    client_ip = frappe.request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or \
                frappe.request.headers.get("X-Real-IP", "") or \
                getattr(frappe.request, "ip", "unknown") or "unknown"
    if not check_rate_limit(client_ip):
        frappe.throw(_("Too many failed attempts. Try again later."), frappe.AuthenticationError)

    settings_secret = frappe.get_single("Settings").get_password(
        "webhook_secret", raise_exception=False
    ) or ""

    vendor_id = frappe.request.headers.get("X-Vendor-ID", "")

    expected = settings_secret
    expected_old = ""
    if vendor_id and frappe.db.exists("Vendor", vendor_id):
        from frappe.utils.password import get_decrypted_password
        try:
            vendor_secret = get_decrypted_password(
                "Vendor", vendor_id, "webhook_secret", raise_exception=False
            ) or ""
        except Exception:
            vendor_secret = ""
        if vendor_secret:
            expected = vendor_secret
        # Zero-downtime rotation: while a rotation is in flight (or its old
        # value hasn't been cleaned up yet), signatures from either secret
        # are valid. The old one is accepted but never used for signing.
        try:
            expected_old = get_decrypted_password(
                "Vendor", vendor_id, "webhook_secret_old", raise_exception=False
            ) or ""
        except Exception:
            expected_old = ""

    if not expected:
        frappe.throw(_("Webhook secret not configured"), frappe.AuthenticationError)

    signature = frappe.request.headers.get("X-SM-Signature", "")
    if signature:
        ts = frappe.request.headers.get("X-SM-Timestamp", "")
        # Timestamp freshness is enforced separately by verify_hub_timestamp;
        # here we only need it as part of the signed message.
        raw_body = frappe.request.get_data(cache=True, as_text=False) or b""
        computed = compute_hmac_signature(expected, ts, raw_body)
        if hmac.compare_digest(signature.strip(), computed):
            clear_failures(client_ip)
            return
        if expected_old:
            computed_old = compute_hmac_signature(expected_old, ts, raw_body)
            if hmac.compare_digest(signature.strip(), computed_old):
                clear_failures(client_ip)
                return
        record_failure(client_ip)
        log_auth_failure(endpoint, "invalid_signature")
        frappe.throw(_("Invalid signature"), frappe.AuthenticationError)

    # No signature header → reject. The legacy bare X-SM-Secret fallback
    # was removed: all callers now send HMAC signatures.
    record_failure(client_ip)
    log_auth_failure(endpoint, "missing_signature")
    frappe.throw(_("Missing webhook signature"), frappe.AuthenticationError)


def verify_hub_timestamp(max_age_seconds=300):
    """
    Reject an inbound vendor push whose X-SM-Timestamp is missing or stale
    (replay-attack guard). Same no-op-without-a-request behaviour as
    verify_hub_secret — see its docstring.
    """
    if not frappe.request:
        return

    ts = frappe.request.headers.get("X-SM-Timestamp")
    if not ts:
        frappe.throw(_("Missing X-SM-Timestamp header"), frappe.AuthenticationError)
    try:
        event_time = float(ts)
    except (TypeError, ValueError):
        frappe.throw(_("Invalid timestamp"), frappe.AuthenticationError)
    if abs(datetime.now(timezone.utc).timestamp() - event_time) > max_age_seconds:
        frappe.throw(_("Request timestamp too old"), frappe.AuthenticationError)


def log_auth_failure(endpoint, reason, payload=None):
    """Log failed authentication attempts with IP, timestamp, payload hash."""
    try:
        ip = frappe.get_request_header("X-Forwarded-For", "").split(",")[0].strip() or \
             frappe.get_request_header("X-Real-IP", "") or \
             (frappe.request.ip if frappe.request else "unknown")
        user_agent = frappe.get_request_header("User-Agent", "")
        payload_hash = ""
        if payload:
            payload_hash = hashlib.sha256(
                json.dumps(payload, default=str).encode()
            ).hexdigest()[:16]
        frappe.log_error(
            title="Webhook Auth Failure",
            message=f"Auth failure: endpoint={endpoint} reason={reason} ip={ip} "
            f"user_agent={user_agent} payload_hash={payload_hash}",
        )
    except Exception:
        pass
