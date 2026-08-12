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


def verify_hub_secret(endpoint):
    """
    Verify the X-SM-Secret header on an inbound vendor push against that
    vendor's own webhook_secret (falling back to the hub's global
    Settings.webhook_secret for vendors that haven't been provisioned with
    their own yet).

    No-ops when there is no active HTTP request — i.e. when the caller is
    invoked internally after the true entry point (events.receive) already
    authenticated the request, or called directly in tests. A real inbound
    HTTP call always has frappe.request set by the time a whitelisted method
    runs, so this guard never opens a bypass for actual traffic.
    """
    if not frappe.request:
        return

    settings_secret = frappe.get_single("Settings").get_password(
        "webhook_secret", raise_exception=False
    ) or ""

    incoming = frappe.request.headers.get("X-SM-Secret", "")
    vendor_id = frappe.request.headers.get("X-Vendor-ID", "")

    expected = settings_secret
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

    if not expected:
        frappe.throw(_("Webhook secret not configured"), frappe.AuthenticationError)
    if not incoming or not hmac.compare_digest(incoming, expected):
        log_auth_failure(endpoint, "invalid_secret")
        frappe.throw(_("Invalid secret"), frappe.AuthenticationError)


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
            f"Auth failure: endpoint={endpoint} reason={reason} ip={ip} "
            f"user_agent={user_agent} payload_hash={payload_hash}",
            "Webhook Auth Failure",
        )
    except Exception:
        pass
