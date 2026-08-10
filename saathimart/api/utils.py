"""
Shared utilities for SaathiMart hub API endpoints.

Single source of truth for:
- Rate limiting
- Request logging
- Common validation helpers
"""
import hashlib
import json

import frappe
from frappe import _


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
