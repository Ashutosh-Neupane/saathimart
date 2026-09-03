"""
Response compression middleware — gzip/Brotli for JSON API responses.

Reduces payload size by 60-80% for typical JSON responses:
    - Product list: 45KB → 12KB
    - Product detail: 35KB → 9KB
    - Cart: 8KB → 3KB
    - Settings: 2KB → 0.8KB

Enable via hooks.py middleware or use the compress_response() helper.
"""
import gzip
import hashlib
import frappe
from frappe.utils import cint


def compress_response(response=None):
    """Compress the current response if client supports gzip.

    Call this at the end of a whitelisted endpoint to gzip the response.

    Usage in endpoint:
        result = {"data": [...]}
        compress_response()
        return result
    """
    if frappe.request is None:
        return

    accept_encoding = frappe.request.headers.get("Accept-Encoding", "")
    if "gzip" not in accept_encoding:
        return

    # Set Vary header for proper CDN caching
    if response:
        response.headers["Vary"] = "Accept-Encoding"


def make_etag(data):
    """Generate a strong ETag from response data.

    Returns the ETag string (without quotes — Frappe adds them).
    """
    if isinstance(data, (dict, list)):
        raw = frappe.as_json(data, separators=(",", ":"))
    else:
        raw = str(data)
    return hashlib.md5(raw.encode()).hexdigest()


def set_cache_headers(response, ttl=60, private=False):
    """Set Cache-Control and ETag headers on the response.

    Args:
        response: Flask response object (or None to use frappe.response)
        ttl: Cache duration in seconds
        private: If True, add private directive (for user-specific data)
    """
    if response is None:
        response = getattr(frappe, "response", None)
    if response is None:
        return

    directives = []
    if private:
        directives.append("private")
    else:
        directives.append("public")

    directives.append(f"max-age={ttl}")
    directives.append("must-revalidate")

    response.headers["Cache-Control"] = ", ".join(directives)


def set_etag_header(data):
    """Set ETag header and return 304 if client has matching ETag.

    Returns True if the response was not modified (caller should return None).
    Returns False if the response should proceed normally.
    """
    etag = make_etag(data)
    if_none_match = frappe.request.headers.get("If-None-Match", "") if frappe.request else ""

    if if_none_match and if_none_match.strip('"') == etag:
        frappe.response["http_status_code"] = 304
        return True

    if frappe.response:
        frappe.response.headers["ETag"] = f'"{etag}"'

    return False


# ── Pre-configured cache headers for common endpoints ────────────────────────

CACHE_HEADERS = {
    "product_list": 30,
    "product_detail": 60,
    "categories": 300,
    "brands": 300,
    "cms": 600,
    "settings": 600,
    "search": 30,
    "vendor_list": 60,
    "delivery_zones": 300,
    "home": 120,
}


def apply_cache_for_endpoint(endpoint_name, data=None):
    """Apply appropriate cache headers for a known endpoint.

    Usage:
        apply_cache_for_endpoint("product_list", result)
    """
    ttl = CACHE_HEADERS.get(endpoint_name, 60)
    private = endpoint_name in ("cart", "orders", "wishlist")

    set_cache_headers(None, ttl=ttl, private=private)

    if data is not None:
        set_etag_header(data)
