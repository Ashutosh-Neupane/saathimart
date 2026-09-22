"""
Shared utilities for SaathiMart hub API endpoints.

Single source of truth for:
- Rate limiting
- Request logging
- Common validation helpers
- Vendor->hub push authentication
"""
import hashlib
from functools import wraps
import hmac
import json
import os
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

    SM_DISABLE_RATE_LIMIT=1 turns every limiter into a no-op — a load-test
    escape hatch (k6 hammers from one IP, which would otherwise trip the
    per-IP buckets long before the system is actually saturated). Never set
    it in production.

    No-ops under frappe.flags.in_test: outside a real HTTP request there's
    no client IP, so guest_rate_limit's caller falls back to a fixed
    "unknown" key — every guest-facing call across the entire test suite
    (hundreds of add_to_cart/checkout/etc. calls within the same 60s
    window) shares that one bucket. Production traffic always has a real
    IP and is unaffected; this only skips the test run hitting its own
    limit and silently failing calls that have nothing to do with what's
    actually being tested.
    """
    if os.environ.get("SM_DISABLE_RATE_LIMIT") == "1":
        return True

    if frappe.flags.in_test:
        return True

    cache_key = f"sm_rate_limit:{key}"
    current = frappe.cache().get_value(cache_key)
    if current is None:
        frappe.cache().set_value(cache_key, 1, expires_in_sec=window_seconds)
        return True
    if current >= limit:
        frappe.throw(_("Rate limit exceeded. Please try again later."))
    frappe.cache().set_value(cache_key, current + 1, expires_in_sec=window_seconds)
    return True


def _get_server_api_token():
    """Decrypted Server API Token (Password field → lives in __Auth, so
    db.get_single_value returns None). Cached 60s — this runs on every
    guest request. Absent/empty token → no bypass, normal rate limits."""
    cache_key = "sm_server_api_token"
    cached = frappe.cache().get_value(cache_key)
    if cached is not None:
        return cached or None
    token = None
    try:
        from frappe.utils.password import get_decrypted_password
        token = get_decrypted_password(
            "SaathiMart Settings", "SaathiMart Settings",
            "server_api_token", raise_exception=False,
        ) or None
    except Exception:
        token = None
    frappe.cache().set_value(cache_key, token or "", expires_in_sec=60)
    return token


def guest_rate_limit(endpoint, limit=60, window_seconds=60):
    """
    Rate limit a guest endpoint by client IP.
    Falls back to 'unknown' if IP cannot be determined.

    Trusted-server bypass: the Next.js server (SSR/prerender) calls these
    same guest endpoints on behalf of EVERY visitor from ONE source IP —
    under the default per-IP key all its users share a single bucket and
    the storefront starts 429-ing under modest traffic. A server that
    presents SaathiMart Settings > Server API Token in the X-Server-Token
    header is treated as trusted infrastructure: no bucket, no limit. The
    token is compared with a constant-time compare and never logged.
    """
    server_token = _get_server_api_token()
    if server_token:
        # No request context (background jobs, bench scripts) → header reads
        # raise RuntimeError; treat as "no token presented" there.
        try:
            presented = frappe.get_request_header("X-Server-Token", "") or ""
        except Exception:
            presented = ""
        if presented and hmac.compare_digest(str(presented), str(server_token)):
            return True
    try:
        ip = frappe.get_request_header("X-Forwarded-For", "").split(",")[0].strip()
        if not ip:
            ip = frappe.get_request_header("X-Real-IP", "")
        if not ip and frappe.request:
            ip = getattr(frappe.request, "ip", "unknown") or "unknown"
    except Exception:
        ip = "unknown"
    return rate_limit(f"{endpoint}:{ip}", limit=limit, window_seconds=window_seconds)


def rate_limited(endpoint, limit=60, window_seconds=60):
    """
    Decorator form of guest_rate_limit. Stack it ABOVE cached_response so
    the limiter sees every request — a cache hit would otherwise absorb
    the call before the limiter ever runs, letting a hot cached URL be
    hammered past its limit for free.

        @frappe.whitelist(allow_guest=True)
        @handle_api_errors
        @rate_limited("products.list", limit=300, window_seconds=60)
        @cached_response(ttl=30, key_prefix="product_list")
        def list_products(...): ...
    """

    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            guest_rate_limit(endpoint, limit=limit, window_seconds=window_seconds)
            return fn(*args, **kwargs)

        return wrapper

    return decorator


def compute_hmac_signature(secret, timestamp, body):
    """
    Stripe-style request signature: HMAC-SHA256 over "<timestamp>.<body>"
    keyed with the shared webhook secret. The secret itself never crosses
    the wire — a captured request reveals nothing reusable.
    """
    msg = f"{timestamp}.".encode() + (body if isinstance(body, bytes) else body.encode())
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


def verify_hub_secret(endpoint, allow_bootstrap=False):
    """
    Authenticate an inbound vendor push.

    Required: X-SM-Signature — HMAC-SHA256(shared_secret, "<ts>.<raw_body>")
    alongside X-SM-Timestamp. The secret never travels, so a leaked header
    or logged request cannot be replayed into a valid credential.

    Requests without a valid HMAC signature are rejected. The legacy bare
    X-SM-Secret header fallback has been removed.

    allow_bootstrap: for the registration handshake only — when the
    per-vendor signature fails, also accept the platform-level
    SaathiMart Settings secret. This exists because a restarted vendor site
    that never received its per-vendor secret can only sign with the shared
    bootstrap value; register_vendor then issues the per-vendor secret so
    every later push verifies per-vendor. Registration is the one endpoint
    where this is safe: it can only claim/refresh a Vendor row's own
    site_url + secret, which is exactly what the shared secret already
    authorizes in update_vendor_location.

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

    settings_secret = frappe.get_single("SaathiMart Settings").get_password(
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
        if allow_bootstrap and settings_secret and settings_secret != expected:
            # Bootstrap fallback for register_vendor only (see docstring).
            computed_boot = compute_hmac_signature(settings_secret, ts, raw_body)
            if hmac.compare_digest(signature.strip(), computed_boot):
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


def verify_hub_timestamp(max_age_seconds=300, vendor_name=None):
    """
    Reject an inbound vendor push whose X-SM-Timestamp is missing or stale
    (replay-attack guard). Same no-op-without-a-request behaviour as
    verify_hub_secret — see its docstring.

    vendor_name, when known (poll/receive/bulk_receive all get it from the
    X-Vendor-ID header), widens max_age_seconds by that vendor's measured
    clock skew — see api/clock_sync.py. Without it this falls back to the
    flat max_age_seconds window, same as before clock_sync existed.

    Also opportunistically re-measures skew from this request's own
    timestamp, so the tolerance keeps adapting as a vendor's clock drifts —
    cheap since this only runs once per authenticated request, not on a
    separate polling cadence.
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

    tolerance = max_age_seconds
    if vendor_name:
        from saathimart.api.clock_sync import get_vendor_clock_skew
        skew = abs(get_vendor_clock_skew(vendor_name))
        tolerance = max(max_age_seconds, skew + 60)

    if abs(datetime.now(timezone.utc).timestamp() - event_time) > tolerance:
        frappe.throw(_("Request timestamp too old"), frappe.AuthenticationError)

    if vendor_name:
        from saathimart.api.clock_sync import measure_clock_skew
        measure_clock_skew(vendor_name, ts)


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


# ── Request Body Size Limit ───────────────────────────────────────────────────

MAX_REQUEST_BODY_BYTES = 1 * 1024 * 1024  # 1 MB


def check_request_size():
    """Reject requests with bodies larger than MAX_REQUEST_BODY_BYTES.

    Call at the top of write-heavy endpoints (add_to_cart, checkout, etc.)
    to prevent memory exhaustion from malicious payloads.
    """
    if not frappe.request:
        return
    content_length = frappe.request.headers.get("Content-Length")
    if content_length and int(content_length) > MAX_REQUEST_BODY_BYTES:
        frappe.throw(
            _("Request body too large (max {0} KB)").format(MAX_REQUEST_BODY_BYTES // 1024),
            frappe.RequestSizeLimitError,
        )


# ── Idempotency Keys ─────────────────────────────────────────────────────────


def check_idempotency(key, ttl_seconds=3600):
    """Check if an idempotency key has already been processed.

    Returns (is_duplicate, existing_result).
    If not duplicate, marks the key as in-progress.
    Call mark_idempotent(key, result) after successful processing.

    Prevents double-charging on payment retries and duplicate order creation.
    """
    cache_key = f"sm_idempotent:{key}"
    existing = frappe.cache().get_value(cache_key)
    if existing is not None:
        return True, existing
    # Mark as in-progress (empty dict)
    frappe.cache().set_value(cache_key, {}, expires_in_sec=ttl_seconds)
    return False, None


def mark_idempotent(key, result, ttl_seconds=3600):
    """Mark an idempotency key as completed with its result."""
    cache_key = f"sm_idempotent:{key}"
    frappe.cache().set_value(cache_key, result or {"ok": True}, expires_in_sec=ttl_seconds)
